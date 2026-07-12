"""Deterministic validation rules (SPEC 7.1, 7.2).

These functions implement the non-LLM half of the evaluator: exact and
fuzzy cross-checks between extracted fields and applicant-declared data,
document expiry and package completeness.
"""

from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher

from kyc_agent.rules.ids import RuleId
from kyc_agent.schemas.case import (
    REQUIRED_DOCS_BY_CUSTOMER,
    ApplicantDeclared,
    CustomerType,
)
from kyc_agent.schemas.decisions import (
    ExtractionResult,
    FieldCheck,
    RuleFlag,
    Severity,
)
from kyc_agent.schemas.documents import REQUIRED_FIELDS_BY_TYPE, DocumentType


@dataclass(frozen=True)
class MatchThresholds:
    """Fuzzy-match thresholds; defaults are the SPEC 7.2 values."""

    name_critical: float = 0.85
    name_warning: float = 0.95
    address: float = 0.70


DEFAULT_MATCH_THRESHOLDS = MatchThresholds()


def normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def fuzzy_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_text(a), normalize_text(b)).ratio()


def check_package_completeness(
    customer_type: CustomerType, present_types: set[DocumentType]
) -> RuleFlag | None:
    missing = REQUIRED_DOCS_BY_CUSTOMER[customer_type] - present_types
    if not missing:
        return None
    return RuleFlag(
        rule_id=RuleId.INCOMPLETE_PACKAGE,
        severity=Severity.CRITICAL,
        details=f"missing required documents: {', '.join(sorted(missing))}",
    )


def check_document_expiry(expiry_date: date | None, today: date) -> RuleFlag | None:
    if expiry_date is None or expiry_date >= today:
        return None
    return RuleFlag(
        rule_id=RuleId.DOC_EXPIRED,
        severity=Severity.CRITICAL,
        details=f"document expired on {expiry_date.isoformat()}",
    )


def check_name(
    declared: str,
    extracted: str | None,
    thresholds: MatchThresholds,
    rule_id: RuleId,
    field: str,
) -> tuple[FieldCheck, RuleFlag | None]:
    """Fuzzy name comparison shared by NAME_MISMATCH and COMPANY_NAME_MISMATCH."""
    if extracted is None:
        # Missing extraction is reported by check_extraction_completeness.
        return (
            FieldCheck(field=field, declared=declared, extracted=None, match=False, critical=False),
            None,
        )
    ratio = fuzzy_ratio(declared, extracted)
    if ratio >= thresholds.name_warning:
        return (
            FieldCheck(
                field=field, declared=declared, extracted=extracted, match=True, critical=False
            ),
            None,
        )
    severity = Severity.CRITICAL if ratio < thresholds.name_critical else Severity.WARNING
    flag = RuleFlag(
        rule_id=rule_id,
        severity=severity,
        details=f"{field}: declared={declared!r} extracted={extracted!r} ratio={ratio:.2f}",
    )
    return (
        FieldCheck(
            field=field,
            declared=declared,
            extracted=extracted,
            match=False,
            critical=severity is Severity.CRITICAL,
        ),
        flag,
    )


def check_exact(
    declared: str | None,
    extracted: str | None,
    rule_id: RuleId,
    field: str,
    *,
    normalize: bool = True,
) -> tuple[FieldCheck | None, RuleFlag | None]:
    """Exact comparison (dates, registration numbers)."""
    if declared is None or extracted is None:
        return None, None
    left, right = (
        (normalize_text(declared), normalize_text(extracted))
        if normalize
        else (declared, extracted)
    )
    if left == right:
        return (
            FieldCheck(
                field=field, declared=declared, extracted=extracted, match=True, critical=False
            ),
            None,
        )
    return (
        FieldCheck(field=field, declared=declared, extracted=extracted, match=False, critical=True),
        RuleFlag(
            rule_id=rule_id,
            severity=Severity.CRITICAL,
            details=f"{field}: declared={declared!r} extracted={extracted!r}",
        ),
    )


def check_address(
    declared: str,
    extracted: str | None,
    thresholds: MatchThresholds,
) -> tuple[FieldCheck | None, RuleFlag | None]:
    if extracted is None:
        return None, None
    ratio = fuzzy_ratio(declared, extracted)
    if ratio >= thresholds.address:
        return (
            FieldCheck(
                field="address", declared=declared, extracted=extracted, match=True, critical=False
            ),
            None,
        )
    return (
        FieldCheck(
            field="address", declared=declared, extracted=extracted, match=False, critical=False
        ),
        RuleFlag(
            rule_id=RuleId.ADDRESS_MISMATCH,
            severity=Severity.WARNING,
            details=f"address: declared={declared!r} extracted={extracted!r} ratio={ratio:.2f}",
        ),
    )


def check_extraction_completeness(extraction: ExtractionResult) -> list[RuleFlag]:
    required = REQUIRED_FIELDS_BY_TYPE.get(extraction.doc_type, ())
    flags: list[RuleFlag] = []
    for field in required:
        value = extraction.fields.get(field)
        if value is None or value == "" or value == []:
            flags.append(
                RuleFlag(
                    rule_id=RuleId.EXTRACTION_INCOMPLETE,
                    severity=Severity.WARNING,
                    details=f"{extraction.doc_type}/{extraction.document_id}: "
                    f"required field {field!r} not extracted",
                )
            )
    return flags


def run_validation_rules(
    applicant: ApplicantDeclared,
    extractions: list[ExtractionResult],
    today: date,
    thresholds: MatchThresholds = DEFAULT_MATCH_THRESHOLDS,
) -> tuple[list[FieldCheck], list[RuleFlag]]:
    """Full deterministic validation pass over all extracted documents."""
    checks: list[FieldCheck] = []
    flags: list[RuleFlag] = []

    for extraction in extractions:
        if extraction.extraction_error is not None:
            continue
        flags.extend(check_extraction_completeness(extraction))
        fields = extraction.fields

        if extraction.doc_type == DocumentType.ID_DOCUMENT:
            check, flag = check_name(
                applicant.full_name,
                fields.get("full_name"),
                thresholds,
                RuleId.NAME_MISMATCH,
                "full_name",
            )
            checks.append(check)
            if flag:
                flags.append(flag)

            declared_dob = applicant.date_of_birth.isoformat() if applicant.date_of_birth else None
            check_opt, flag = check_exact(
                declared_dob, fields.get("date_of_birth"), RuleId.DOB_MISMATCH, "date_of_birth"
            )
            if check_opt:
                checks.append(check_opt)
            if flag:
                flags.append(flag)

            expiry_raw = fields.get("expiry_date")
            expiry = date.fromisoformat(expiry_raw) if expiry_raw else None
            expiry_flag = check_document_expiry(expiry, today)
            if expiry_flag:
                flags.append(expiry_flag)

        elif extraction.doc_type == DocumentType.PROOF_OF_ADDRESS:
            check_opt, flag = check_address(applicant.address, fields.get("address"), thresholds)
            if check_opt:
                checks.append(check_opt)
            if flag:
                flags.append(flag)

        elif extraction.doc_type == DocumentType.BUSINESS_REGISTRATION:
            if applicant.company_name:
                check, flag = check_name(
                    applicant.company_name,
                    fields.get("company_name"),
                    thresholds,
                    RuleId.COMPANY_NAME_MISMATCH,
                    "company_name",
                )
                checks.append(check)
                if flag:
                    flags.append(flag)
            check_opt, flag = check_exact(
                applicant.registration_number,
                fields.get("registration_number"),
                RuleId.REG_NUMBER_MISMATCH,
                "registration_number",
            )
            if check_opt:
                checks.append(check_opt)
            if flag:
                flags.append(flag)

    # Package completeness is the orchestrator's concern (it runs before
    # extraction, SPEC 8.1); it is deliberately not re-checked here.
    return checks, flags
