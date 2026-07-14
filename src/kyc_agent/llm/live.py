"""LLM-backed implementations of the step services.

Provider-agnostic: models come from LangChain ``init_chat_model`` with
``provider:model`` identifiers, temperature 0 everywhere (SPEC 4.3), and
structured output pinned to the Pydantic schemas from ``kyc_agent.schemas``.
"""

from typing import Any, cast

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field, create_model

from kyc_agent.llm.base import (
    EvaluatorService,
    ExtractorService,
    RiskNarratorService,
    RouterService,
)
from kyc_agent.rules.ids import RuleId
from kyc_agent.schemas.case import ApplicantDeclared, CustomerType
from kyc_agent.schemas.decisions import ExtractionResult, RuleFlag, Severity
from kyc_agent.schemas.documents import (
    EXTRACTION_SCHEMA_BY_TYPE,
    ClassifiedDocument,
    DocumentType,
    InputDocument,
)

_MAX_DOC_CHARS = 6_000  # hard cap per document to bound prompt size


class _DocClassification(BaseModel):
    document_id: str
    doc_type: DocumentType
    confidence: float = Field(ge=0.0, le=1.0)


class _RouterOutput(BaseModel):
    classifications: list[_DocClassification]


_ROUTER_SYSTEM = (
    "You are a KYC document classifier for a fintech compliance pipeline. "
    "Classify every document into exactly one type: id_document, "
    "proof_of_address, business_registration, ubo_declaration or unknown. "
    "Use 'unknown' when the text is unreadable or fits no type. "
    "Return a classification for every document_id you were given."
)


class LiveRouter(RouterService):
    def __init__(self, model: BaseChatModel) -> None:
        self._model = model.with_structured_output(_RouterOutput)

    async def classify(
        self, documents: list[InputDocument], customer_type: CustomerType
    ) -> list[ClassifiedDocument]:
        blocks = [
            f"<document id={d.document_id!r} file={d.file_name!r}>\n"
            f"{d.text_content[:_MAX_DOC_CHARS]}\n</document>"
            for d in documents
        ]
        prompt = f"Customer type declared at registration: {customer_type}.\n\n" + "\n\n".join(
            blocks
        )
        result = cast(
            "_RouterOutput",
            await self._model.ainvoke([("system", _ROUTER_SYSTEM), ("user", prompt)]),
        )
        by_id = {c.document_id: c for c in result.classifications}
        classified: list[ClassifiedDocument] = []
        for doc in documents:
            item = by_id.get(doc.document_id)
            classified.append(
                ClassifiedDocument(
                    document_id=doc.document_id,
                    doc_type=item.doc_type if item else DocumentType.UNKNOWN,
                    classifier_confidence=item.confidence if item else 0.0,
                )
            )
        return classified


_EXTRACTOR_SYSTEM = (
    "You are a KYC field extractor. Extract fields from the document text "
    "into the given schema. Rules: use null for any field you cannot find "
    "verbatim in the document — never guess or infer; copy values exactly "
    "as written; convert dates to ISO YYYY-MM-DD; report a confidence in "
    "[0,1] for every extracted field."
)


class LiveExtractor(ExtractorService):
    def __init__(self, model: BaseChatModel) -> None:
        self._model = model

    async def extract(self, document: InputDocument, doc_type: DocumentType) -> ExtractionResult:
        schema = EXTRACTION_SCHEMA_BY_TYPE[doc_type]
        wrapper = create_model(
            f"{schema.__name__}Envelope",
            fields=(schema, ...),
            field_confidence=(dict[str, float], Field(default_factory=dict)),
        )
        structured = self._model.with_structured_output(wrapper)
        result: Any = await structured.ainvoke(
            [
                ("system", _EXTRACTOR_SYSTEM),
                ("user", f"Document type: {doc_type}\n\n{document.text_content[:_MAX_DOC_CHARS]}"),
            ]
        )
        extracted: BaseModel = result.fields
        fields = {k: v for k, v in extracted.model_dump(mode="json").items() if v is not None}
        if not fields:
            return ExtractionResult(
                document_id=document.document_id,
                doc_type=doc_type,
                extraction_error="unreadable document: extractor found no fields",
            )
        return ExtractionResult(
            document_id=document.document_id,
            doc_type=doc_type,
            fields=fields,
            field_confidence={k: v for k, v in result.field_confidence.items() if k in fields},
        )


class _Discrepancy(BaseModel):
    field: str
    extracted_value: str
    reason: str


class _GroundingReport(BaseModel):
    discrepancies: list[_Discrepancy] = Field(default_factory=list)


_EVALUATOR_SYSTEM = (
    "You are a compliance QA agent verifying another agent's extraction. "
    "For every extracted field value, check it literally appears in the "
    "source document (allowing case and whitespace differences; for dates "
    "allow format differences). Report a discrepancy for every value that "
    "is NOT grounded in the document. Report nothing else."
)


class LiveEvaluator(EvaluatorService):
    def __init__(self, model: BaseChatModel) -> None:
        self._model = model.with_structured_output(_GroundingReport)

    async def verify_grounding(
        self, document: InputDocument, extraction: ExtractionResult
    ) -> list[RuleFlag]:
        prompt = (
            f"<document>\n{document.text_content[:_MAX_DOC_CHARS]}\n</document>\n\n"
            f"<extraction>\n{extraction.model_dump_json(indent=2)}\n</extraction>"
        )
        report = cast(
            "_GroundingReport",
            await self._model.ainvoke([("system", _EVALUATOR_SYSTEM), ("user", prompt)]),
        )
        return [
            RuleFlag(
                rule_id=RuleId.EVALUATOR_DISCREPANCY,
                severity=Severity.CRITICAL,
                details=(
                    f"{extraction.doc_type}/{extraction.document_id}: "
                    f"{d.field}={d.extracted_value!r} — {d.reason}"
                ),
            )
            for d in report.discrepancies
        ]


_NARRATOR_SYSTEM = (
    "You are writing an audit-trail rationale for a KYC decision. "
    "Summarize the triggered rules and validation flags in precise, "
    "neutral compliance language. Do not invent facts; reference only "
    "the provided flags. 120 words maximum."
)


class LiveRiskNarrator(RiskNarratorService):
    def __init__(self, model: BaseChatModel) -> None:
        self._model = model

    async def narrate(
        self,
        applicant: ApplicantDeclared,
        triggered_rules: list[RuleFlag],
        validation_flags: list[RuleFlag],
    ) -> str:
        lines = [f"Applicant: {applicant.full_name}"]
        lines += [f"TRIGGER {f.rule_id}: {f.details}" for f in triggered_rules]
        lines += [f"FLAG {f.rule_id} [{f.severity}]: {f.details}" for f in validation_flags]
        response = await self._model.ainvoke(
            [("system", _NARRATOR_SYSTEM), ("user", "\n".join(lines))]
        )
        return str(response.content)
