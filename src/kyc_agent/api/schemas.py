"""API request/response DTOs (SPEC 10)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from kyc_agent.schemas.case import CaseStatus
from kyc_agent.schemas.decisions import Decision


class SubmitCaseResponse(BaseModel):
    case_id: str
    status: CaseStatus


class CaseStatusResponse(BaseModel):
    case_id: str
    status: CaseStatus
    decision: Decision | None = None
    risk_level: str | None = None
    overall_confidence: float | None = None
    degraded: bool = False
    degraded_reasons: list[str] = Field(default_factory=list)
    # Present only while the case waits for a human (SPEC 8.3 payload).
    review_request: dict[str, Any] | None = None


class AuditEventResponse(BaseModel):
    ts: datetime
    node: str
    event_type: str
    payload: dict[str, Any]


class HealthResponse(BaseModel):
    status: str
    database: str
