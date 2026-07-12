"""Deterministic offline implementations of the step services (SPEC 9).

First-class runtime mode for tests, CI, eval and keyless demo: rule/regex
logic stands in for the LLM behind the exact same interfaces, so the graph
path is identical to the live mode.

Fault-injection markers understood by this module (synthetic docs only):
- a document with no recognizable labels -> extraction error (unreadable);
- ``[OCR-GLITCH:<field>=<value>]`` -> the extractor reports ``<value>`` for
  ``<field>`` regardless of the document body, simulating a hallucination
  that only the evaluator's grounding check can catch (used to measure the
  validator's contribution in the eval ablation, SPEC 11).
"""

import re
from datetime import date

from kyc_agent.llm.base import (
    EvaluatorService,
    ExtractorService,
    RiskNarratorService,
    RouterService,
)
from kyc_agent.rules.ids import RuleId
from kyc_agent.schemas.case import ApplicantDeclared, CustomerType
from kyc_agent.schemas.decisions import ExtractionResult, RuleFlag, Severity
from kyc_agent.schemas.documents import ClassifiedDocument, DocumentType, InputDocument

_GLITCH_RE = re.compile(r"\[OCR-GLITCH:(?P<field>[a-z_]+)=(?P<value>[^\]]+)\]")

_TYPE_KEYWORDS: tuple[tuple[DocumentType, tuple[str, ...]], ...] = (
    (DocumentType.UBO_DECLARATION, ("UBO DECLARATION", "BENEFICIAL OWNER")),
    (
        DocumentType.BUSINESS_REGISTRATION,
        ("CERTIFICATE OF REGISTRATION", "COMMERCIAL REGISTER", "BUSINESS REGISTRY"),
    ),
    (DocumentType.ID_DOCUMENT, ("IDENTITY CARD", "PASSPORT", "RESIDENCE PERMIT")),
    (
        DocumentType.PROOF_OF_ADDRESS,
        ("UTILITY", "INVOICE", "BANK STATEMENT", "ELECTRICITY", "LEASE AGREEMENT"),
    ),
)

_LABELS: dict[str, tuple[str, ...]] = {
    "full_name": ("full name", "name", "customer", "account holder", "surname and given names"),
    "date_of_birth": ("date of birth", "born"),
    "document_number": ("document number", "card number", "passport number"),
    "expiry_date": ("date of expiry", "expiry date", "valid until"),
    "nationality": ("nationality",),
    "address": ("service address", "supply address", "address"),
    "issue_date": ("invoice date", "issue date", "statement date"),
    "issuer": ("issuer",),
    "company_name": ("company name", "legal name"),
    "registration_number": ("registration number", "registry code"),
    "registration_date": ("registration date", "registered on"),
    "legal_form": ("legal form",),
    "registered_address": ("registered office", "registered address"),
}

_FIELDS_BY_TYPE: dict[DocumentType, tuple[str, ...]] = {
    DocumentType.ID_DOCUMENT: (
        "full_name",
        "date_of_birth",
        "document_number",
        "expiry_date",
        "nationality",
    ),
    DocumentType.PROOF_OF_ADDRESS: ("full_name", "address", "issue_date", "issuer"),
    DocumentType.BUSINESS_REGISTRATION: (
        "company_name",
        "registration_number",
        "registration_date",
        "legal_form",
        "registered_address",
    ),
    DocumentType.UBO_DECLARATION: ("company_name",),
}

_DATE_FIELDS = frozenset({"date_of_birth", "expiry_date", "issue_date", "registration_date"})

_OWNER_RE = re.compile(
    r"^[-•*]\s*(?P<name>[^;]+?)\s*;\s*born\s+(?P<dob>[\d.\-]+)\s*;\s*ownership\s+(?P<pct>[\d.]+)\s*%",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_date(raw: str) -> str | None:
    raw = raw.strip()
    for pattern, order in (
        (r"(\d{4})-(\d{2})-(\d{2})", "ymd"),
        (r"(\d{2})\.(\d{2})\.(\d{4})", "dmy"),
    ):
        m = re.fullmatch(pattern, raw)
        if m:
            parts = m.groups()
            y, mo, d = parts if order == "ymd" else (parts[2], parts[1], parts[0])
            try:
                return date(int(y), int(mo), int(d)).isoformat()
            except ValueError:
                return None
    return None


def _find_label(text: str, labels: tuple[str, ...]) -> str | None:
    for label in labels:
        m = re.search(
            rf"^{re.escape(label)}\s*:\s*(?P<value>.+)$", text, re.IGNORECASE | re.MULTILINE
        )
        if m:
            return m.group("value").strip()
    return None


class FakeRouter(RouterService):
    async def classify(
        self, documents: list[InputDocument], customer_type: CustomerType
    ) -> list[ClassifiedDocument]:
        results: list[ClassifiedDocument] = []
        for doc in documents:
            upper = doc.text_content.upper()
            doc_type = DocumentType.UNKNOWN
            confidence = 0.2
            for candidate, keywords in _TYPE_KEYWORDS:
                if any(k in upper for k in keywords):
                    doc_type = candidate
                    confidence = 0.98
                    break
            results.append(
                ClassifiedDocument(
                    document_id=doc.document_id,
                    doc_type=doc_type,
                    classifier_confidence=confidence,
                )
            )
        return results


class FakeExtractor(ExtractorService):
    async def extract(self, document: InputDocument, doc_type: DocumentType) -> ExtractionResult:
        text = document.text_content
        fields: dict[str, object] = {}
        confidence: dict[str, float] = {}

        for field in _FIELDS_BY_TYPE.get(doc_type, ()):
            raw = _find_label(text, _LABELS[field])
            if raw is None:
                continue
            value: object | None = _parse_date(raw) if field in _DATE_FIELDS else raw
            if value is not None:
                fields[field] = value
                confidence[field] = 0.98

        if doc_type == DocumentType.UBO_DECLARATION:
            owners = [
                {
                    "full_name": m.group("name").strip(),
                    "date_of_birth": _parse_date(m.group("dob")),
                    "ownership_percent": float(m.group("pct")),
                }
                for m in _OWNER_RE.finditer(text)
            ]
            if owners:
                fields["beneficial_owners"] = owners
                confidence["beneficial_owners"] = 0.98

        # Hallucination injection for the evaluator ablation (see module doc).
        for glitch in _GLITCH_RE.finditer(text):
            fields[glitch.group("field")] = glitch.group("value").strip()
            confidence[glitch.group("field")] = 0.98

        if not fields:
            return ExtractionResult(
                document_id=document.document_id,
                doc_type=doc_type,
                extraction_error="unreadable document: no recognizable fields",
            )

        return ExtractionResult(
            document_id=document.document_id,
            doc_type=doc_type,
            fields=fields,
            field_confidence=confidence,
        )


class FakeEvaluator(EvaluatorService):
    """Grounding check: every extracted string must occur in the source text.

    Date fields are skipped on purpose (format variance between document
    and ISO representation); string fields are where hallucinations bite.
    """

    async def verify_grounding(
        self, document: InputDocument, extraction: ExtractionResult
    ) -> list[RuleFlag]:
        # Strip fault-injection markers: they are synthetic metadata, not
        # document content, and must not count as grounding evidence.
        source_text = _GLITCH_RE.sub(" ", document.text_content)
        haystack = " ".join(source_text.casefold().split())
        flags: list[RuleFlag] = []

        def grounded(value: str) -> bool:
            return " ".join(value.casefold().split()) in haystack

        for field, value in extraction.fields.items():
            if field in _DATE_FIELDS:
                continue
            if isinstance(value, str) and not grounded(value):
                flags.append(
                    RuleFlag(
                        rule_id=RuleId.EVALUATOR_DISCREPANCY,
                        severity=Severity.CRITICAL,
                        details=(
                            f"{extraction.doc_type}/{extraction.document_id}: extracted "
                            f"{field}={value!r} not found in the source document"
                        ),
                    )
                )
            if field == "beneficial_owners" and isinstance(value, list):
                for owner in value:
                    name = owner.get("full_name", "") if isinstance(owner, dict) else ""
                    if name and not grounded(name):
                        flags.append(
                            RuleFlag(
                                rule_id=RuleId.EVALUATOR_DISCREPANCY,
                                severity=Severity.CRITICAL,
                                details=(
                                    f"{extraction.doc_type}/{extraction.document_id}: "
                                    f"beneficial owner {name!r} not found in the source document"
                                ),
                            )
                        )
        return flags


class FakeRiskNarrator(RiskNarratorService):
    async def narrate(
        self,
        applicant: ApplicantDeclared,
        triggered_rules: list[RuleFlag],
        validation_flags: list[RuleFlag],
    ) -> str:
        if not triggered_rules and not validation_flags:
            return (
                f"No risk triggers or validation flags for {applicant.full_name}; "
                "case qualifies for automatic processing."
            )
        parts = [f"Risk summary for {applicant.full_name}:"]
        parts += [f"- [{f.severity}] {f.rule_id}: {f.details}" for f in triggered_rules]
        parts += [f"- [{f.severity}] {f.rule_id}: {f.details}" for f in validation_flags]
        return "\n".join(parts)
