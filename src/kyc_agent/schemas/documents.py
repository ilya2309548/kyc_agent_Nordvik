"""Document types and structured extraction schemas (SPEC 6.1, 6.3)."""

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field


class DocumentType(StrEnum):
    ID_DOCUMENT = "id_document"
    PROOF_OF_ADDRESS = "proof_of_address"
    BUSINESS_REGISTRATION = "business_registration"
    UBO_DECLARATION = "ubo_declaration"
    UNKNOWN = "unknown"


class InputDocument(BaseModel):
    """A single uploaded document; ``text_content`` is upstream OCR output."""

    document_id: str
    file_name: str
    text_content: str


class ClassifiedDocument(BaseModel):
    document_id: str
    doc_type: DocumentType
    classifier_confidence: float = Field(ge=0.0, le=1.0)


# --- Structured-output schemas, one per document type (SPEC 6.3) ---
# All fields are optional: the extractor must return None for a field it
# cannot find rather than invent a value; completeness is a validation rule.


class IndividualIdFields(BaseModel):
    full_name: str | None = None
    date_of_birth: date | None = None
    document_number: str | None = None
    expiry_date: date | None = None
    nationality: str | None = None


class ProofOfAddressFields(BaseModel):
    full_name: str | None = None
    address: str | None = None
    issue_date: date | None = None
    issuer: str | None = None


class BusinessRegistrationFields(BaseModel):
    company_name: str | None = None
    registration_number: str | None = None
    registration_date: date | None = None
    legal_form: str | None = None
    registered_address: str | None = None


class BeneficialOwner(BaseModel):
    full_name: str
    date_of_birth: date | None = None
    ownership_percent: float | None = Field(default=None, ge=0.0, le=100.0)


class UboDeclarationFields(BaseModel):
    company_name: str | None = None
    beneficial_owners: list[BeneficialOwner] = Field(default_factory=list)


EXTRACTION_SCHEMA_BY_TYPE: dict[DocumentType, type[BaseModel]] = {
    DocumentType.ID_DOCUMENT: IndividualIdFields,
    DocumentType.PROOF_OF_ADDRESS: ProofOfAddressFields,
    DocumentType.BUSINESS_REGISTRATION: BusinessRegistrationFields,
    DocumentType.UBO_DECLARATION: UboDeclarationFields,
}

REQUIRED_FIELDS_BY_TYPE: dict[DocumentType, tuple[str, ...]] = {
    DocumentType.ID_DOCUMENT: ("full_name", "date_of_birth", "document_number", "expiry_date"),
    DocumentType.PROOF_OF_ADDRESS: ("full_name", "address"),
    DocumentType.BUSINESS_REGISTRATION: ("company_name", "registration_number"),
    DocumentType.UBO_DECLARATION: ("company_name", "beneficial_owners"),
}
