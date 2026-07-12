"""KYC case input model and lifecycle statuses (SPEC 6.1, 6.4, 7.1)."""

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from kyc_agent.schemas.documents import DocumentType, InputDocument


class CustomerType(StrEnum):
    INDIVIDUAL = "individual"
    BUSINESS = "business"


class CaseStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    AWAITING_HUMAN_REVIEW = "awaiting_human_review"
    COMPLETED = "completed"
    FAILED = "failed"


class ApplicantDeclared(BaseModel):
    """Data the customer entered at registration; ground for cross-checks."""

    full_name: str
    date_of_birth: date | None = None
    address: str
    company_name: str | None = None
    registration_number: str | None = None
    expected_monthly_volume_eur: Decimal = Field(ge=0)


class KYCPackage(BaseModel):
    customer_type: CustomerType
    applicant: ApplicantDeclared
    documents: list[InputDocument] = Field(min_length=1)


REQUIRED_DOCS_BY_CUSTOMER: dict[CustomerType, frozenset[DocumentType]] = {
    CustomerType.INDIVIDUAL: frozenset(
        {DocumentType.ID_DOCUMENT, DocumentType.PROOF_OF_ADDRESS}
    ),
    CustomerType.BUSINESS: frozenset(
        {DocumentType.BUSINESS_REGISTRATION, DocumentType.UBO_DECLARATION}
    ),
}
