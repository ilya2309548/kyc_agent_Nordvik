"""Step-service interfaces the graph nodes depend on.

Nodes never talk to a chat model directly: each LLM-powered step is a
narrow typed service. Implementations: ``llm.live`` (real providers via
LangChain ``init_chat_model``) and ``llm.fake`` (deterministic offline
mode, SPEC section 9). The factory picks one per step from settings.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from kyc_agent.schemas.case import ApplicantDeclared, CustomerType
from kyc_agent.schemas.decisions import ExtractionResult, RuleFlag
from kyc_agent.schemas.documents import ClassifiedDocument, DocumentType, InputDocument


class PipelineStep(StrEnum):
    ROUTER = "router"
    EXTRACTOR = "extractor"
    VALIDATOR = "validator"
    RISK = "risk"


# Deterministic generation everywhere: extraction, classification and
# fact-checking must be reproducible (SPEC 4.3).
TEMPERATURE_BY_STEP: dict[PipelineStep, float] = {
    PipelineStep.ROUTER: 0.0,
    PipelineStep.EXTRACTOR: 0.0,
    PipelineStep.VALIDATOR: 0.0,
    PipelineStep.RISK: 0.0,
}


class RouterService(Protocol):
    async def classify(
        self, documents: list[InputDocument], customer_type: CustomerType
    ) -> list[ClassifiedDocument]: ...


class ExtractorService(Protocol):
    async def extract(
        self, document: InputDocument, doc_type: DocumentType
    ) -> ExtractionResult: ...


class EvaluatorService(Protocol):
    """Agent-checks-agent (SPEC 4.4): grounding check of extractor output."""

    async def verify_grounding(
        self, document: InputDocument, extraction: ExtractionResult
    ) -> list[RuleFlag]: ...


class RiskNarratorService(Protocol):
    """Builds the human-readable audit rationale for the risk assessment."""

    async def narrate(
        self,
        applicant: ApplicantDeclared,
        triggered_rules: list[RuleFlag],
        validation_flags: list[RuleFlag],
    ) -> str: ...


@dataclass(frozen=True)
class PipelineServices:
    router: RouterService
    extractor: ExtractorService
    evaluator: EvaluatorService
    narrator: RiskNarratorService
