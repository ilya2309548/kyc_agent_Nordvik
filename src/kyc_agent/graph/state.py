"""Graph state (SPEC 6.2).

The state is checkpointed on every superstep; everything in it must be
JSON-serializable through the LangGraph serde (Pydantic models are).
``extractions`` and ``errors`` use additive reducers because extraction
workers fan in concurrently (Send API), and ``retry_counts`` merges
per-node attempt counters.
"""

import operator
from typing import Annotated, TypedDict

from kyc_agent.schemas.case import CaseStatus, KYCPackage
from kyc_agent.schemas.decisions import (
    Decision,
    ExtractionResult,
    ProcessingError,
    RiskAssessment,
    RuleFlag,
    ValidationReport,
)
from kyc_agent.schemas.documents import ClassifiedDocument, DocumentType, InputDocument


def _merge_counts(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    merged = dict(left)
    for key, value in right.items():
        merged[key] = merged.get(key, 0) + value
    return merged


class KYCState(TypedDict, total=False):
    case_id: str
    package: KYCPackage
    status: CaseStatus

    classified_documents: list[ClassifiedDocument]
    completeness_flag: RuleFlag | None
    extractions: Annotated[list[ExtractionResult], operator.add]
    validation: ValidationReport | None
    risk: RiskAssessment | None
    decision: Decision | None

    errors: Annotated[list[ProcessingError], operator.add]
    retry_counts: Annotated[dict[str, int], _merge_counts]
    # The case is degraded iff this list is non-empty; workers append
    # concurrently, hence a list reducer instead of a bare bool.
    degraded_reasons: Annotated[list[str], operator.add]


class ExtractTask(TypedDict):
    """Send-payload for one extraction worker (SPEC 4.2)."""

    case_id: str
    document: InputDocument
    doc_type: DocumentType
