"""Pipeline result models: extraction, validation, risk, decision (SPEC 6.2)."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from kyc_agent.schemas.documents import (
    EXTRACTION_SCHEMA_BY_TYPE,
    DocumentType,
)


class Severity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class RuleFlag(BaseModel):
    rule_id: str
    severity: Severity
    details: str


class FieldCheck(BaseModel):
    """One declared-vs-extracted comparison performed by the validator."""

    field: str
    declared: str | None
    extracted: str | None
    match: bool
    critical: bool


class ExtractionResult(BaseModel):
    """Extractor worker output for one document.

    ``fields`` holds JSON-native values (dates as ISO strings) so the graph
    state stays trivially serializable for the checkpointer; use
    :meth:`typed_fields` to get the validated per-type schema back.
    """

    document_id: str
    doc_type: DocumentType
    fields: dict[str, Any] = Field(default_factory=dict)
    field_confidence: dict[str, float] = Field(default_factory=dict)
    extraction_error: str | None = None

    def typed_fields(self) -> Any:
        schema = EXTRACTION_SCHEMA_BY_TYPE.get(self.doc_type)
        if schema is None:
            return None
        return schema.model_validate(self.fields)


class ValidationReport(BaseModel):
    field_checks: list[FieldCheck] = Field(default_factory=list)
    rule_flags: list[RuleFlag] = Field(default_factory=list)
    overall_confidence: float = Field(ge=0.0, le=1.0)
    evaluator_notes: str = ""


class RegistryHit(BaseModel):
    registry: Literal["sanctions", "pep"]
    matched_name: str
    score: float = Field(ge=0.0, le=1.0)
    list_entry: str


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskAssessment(BaseModel):
    level: RiskLevel
    triggered_rules: list[RuleFlag] = Field(default_factory=list)
    sanctions_hits: list[RegistryHit] = Field(default_factory=list)
    pep_hits: list[RegistryHit] = Field(default_factory=list)
    rationale: str = ""


class DecisionOutcome(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    ESCALATE = "escalate"


class DecidedBy(StrEnum):
    SYSTEM = "system"
    HUMAN = "human"


class Decision(BaseModel):
    outcome: DecisionOutcome
    decided_by: DecidedBy
    reason_codes: list[str] = Field(default_factory=list)
    rationale: str = ""
    reviewer: str | None = None


class HumanDecision(BaseModel):
    """Resume payload supplied by the compliance analyst (SPEC 8.3)."""

    outcome: Literal["approve", "reject"]
    reviewer: str
    comment: str = ""


class ProcessingError(BaseModel):
    node: str
    error_type: str
    message: str
    attempt: int = 1
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
