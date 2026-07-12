"""Stable rule identifiers used in flags, reason codes and the audit trail."""

from enum import StrEnum


class RuleId(StrEnum):
    # Validation rules (SPEC 7.2)
    INCOMPLETE_PACKAGE = "INCOMPLETE_PACKAGE"
    DOC_EXPIRED = "DOC_EXPIRED"
    NAME_MISMATCH = "NAME_MISMATCH"
    DOB_MISMATCH = "DOB_MISMATCH"
    ADDRESS_MISMATCH = "ADDRESS_MISMATCH"
    COMPANY_NAME_MISMATCH = "COMPANY_NAME_MISMATCH"
    REG_NUMBER_MISMATCH = "REG_NUMBER_MISMATCH"
    EXTRACTION_INCOMPLETE = "EXTRACTION_INCOMPLETE"
    EVALUATOR_DISCREPANCY = "EVALUATOR_DISCREPANCY"

    # Risk rules — mandatory escalation triggers (SPEC 7.3)
    SANCTIONS_HIT = "SANCTIONS_HIT"
    PEP_MATCH = "PEP_MATCH"
    HIGH_VOLUME = "HIGH_VOLUME"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    CRITICAL_MISMATCH = "CRITICAL_MISMATCH"
    REGISTRY_UNAVAILABLE = "REGISTRY_UNAVAILABLE"
    UBO_SANCTIONS_OR_PEP = "UBO_SANCTIONS_OR_PEP"

    # Decision reason codes that are not rules per se
    DOCUMENT_EXPIRED = "DOCUMENT_EXPIRED"
    ALL_CHECKS_PASSED = "ALL_CHECKS_PASSED"
    DEGRADED_TO_MANUAL = "DEGRADED_TO_MANUAL"


# Critical validation flags that count as CRITICAL_MISMATCH escalation
# triggers. DOC_EXPIRED and INCOMPLETE_PACKAGE are excluded on purpose:
# they lead to deterministic auto-rejects (SPEC 7.4).
CRITICAL_MISMATCH_SOURCES: frozenset[RuleId] = frozenset(
    {
        RuleId.NAME_MISMATCH,
        RuleId.DOB_MISMATCH,
        RuleId.COMPANY_NAME_MISMATCH,
        RuleId.REG_NUMBER_MISMATCH,
        RuleId.EVALUATOR_DISCREPANCY,
    }
)
