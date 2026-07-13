"""Risk rules — mandatory escalation triggers — and the decision matrix
(SPEC 7.3, 7.4).

Invariant: if at least one escalation trigger fired, auto-approval is
impossible under any circumstances (escalation recall == 1.0 by design).
"""

from dataclasses import dataclass
from decimal import Decimal

from kyc_agent.rules.ids import CRITICAL_MISMATCH_SOURCES, RuleId
from kyc_agent.schemas.case import CustomerType
from kyc_agent.schemas.decisions import (
    DecidedBy,
    Decision,
    DecisionOutcome,
    RegistryHit,
    RiskLevel,
    RuleFlag,
    Severity,
)


@dataclass(frozen=True)
class RiskThresholds:
    """Escalation thresholds; defaults are the SPEC 7.3 values."""

    high_volume_individual_eur: Decimal = Decimal(10_000)
    high_volume_business_eur: Decimal = Decimal(50_000)
    confidence: float = 0.75


DEFAULT_RISK_THRESHOLDS = RiskThresholds()


def evaluate_risk_rules(
    customer_type: CustomerType,
    expected_monthly_volume_eur: Decimal,
    overall_confidence: float,
    validation_flags: list[RuleFlag],
    sanctions_hits: list[RegistryHit],
    pep_hits: list[RegistryHit],
    ubo_hits: list[RegistryHit],
    registry_unavailable: list[str],
    thresholds: RiskThresholds = DEFAULT_RISK_THRESHOLDS,
) -> list[RuleFlag]:
    """Return every fired mandatory-escalation trigger (SPEC 7.3)."""
    triggered: list[RuleFlag] = []

    if sanctions_hits:
        names = ", ".join(f"{h.matched_name} ({h.score:.2f})" for h in sanctions_hits)
        triggered.append(
            RuleFlag(
                rule_id=RuleId.SANCTIONS_HIT,
                severity=Severity.CRITICAL,
                details=f"sanctions registry match: {names}",
            )
        )

    if pep_hits:
        names = ", ".join(f"{h.matched_name} ({h.score:.2f})" for h in pep_hits)
        triggered.append(
            RuleFlag(
                rule_id=RuleId.PEP_MATCH,
                severity=Severity.CRITICAL,
                details=f"PEP registry match: {names}",
            )
        )

    if ubo_hits:
        names = ", ".join(f"{h.registry}:{h.matched_name}" for h in ubo_hits)
        triggered.append(
            RuleFlag(
                rule_id=RuleId.UBO_SANCTIONS_OR_PEP,
                severity=Severity.CRITICAL,
                details=f"beneficial owner registry match: {names}",
            )
        )

    limit = (
        thresholds.high_volume_individual_eur
        if customer_type == CustomerType.INDIVIDUAL
        else thresholds.high_volume_business_eur
    )
    if expected_monthly_volume_eur > limit:
        triggered.append(
            RuleFlag(
                rule_id=RuleId.HIGH_VOLUME,
                severity=Severity.CRITICAL,
                details=(
                    f"expected monthly volume {expected_monthly_volume_eur} EUR "
                    f"exceeds {limit} EUR threshold for {customer_type}"
                ),
            )
        )

    if overall_confidence < thresholds.confidence:
        triggered.append(
            RuleFlag(
                rule_id=RuleId.LOW_CONFIDENCE,
                severity=Severity.CRITICAL,
                details=(
                    f"overall confidence {overall_confidence:.2f} below {thresholds.confidence:.2f}"
                ),
            )
        )

    critical_mismatches = [
        f
        for f in validation_flags
        if RuleId(f.rule_id) in CRITICAL_MISMATCH_SOURCES and f.severity is Severity.CRITICAL
    ]
    if critical_mismatches:
        ids = ", ".join(sorted({f.rule_id for f in critical_mismatches}))
        triggered.append(
            RuleFlag(
                rule_id=RuleId.CRITICAL_MISMATCH,
                severity=Severity.CRITICAL,
                details=f"critical mismatch between declared and extracted data: {ids}",
            )
        )

    for registry in registry_unavailable:
        triggered.append(
            RuleFlag(
                rule_id=RuleId.REGISTRY_UNAVAILABLE,
                severity=Severity.CRITICAL,
                details=f"{registry} registry unavailable after retries; "
                "'no answer' must not be treated as 'clean'",
            )
        )

    return triggered


def risk_level(triggered: list[RuleFlag]) -> RiskLevel:
    high_risk_ids = {
        RuleId.SANCTIONS_HIT,
        RuleId.PEP_MATCH,
        RuleId.UBO_SANCTIONS_OR_PEP,
        RuleId.REGISTRY_UNAVAILABLE,
    }
    if any(RuleId(f.rule_id) in high_risk_ids for f in triggered):
        return RiskLevel.HIGH
    if triggered:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def decide_gate(
    completeness_flag: RuleFlag | None,
    validation_flags: list[RuleFlag],
    escalation_triggers: list[RuleFlag],
    degraded: bool,
) -> Decision:
    """Deterministic decision matrix (SPEC 7.4), evaluated top-down."""
    if completeness_flag is not None and not degraded:
        # A degraded package (e.g. unreadable document) is never auto-rejected
        # as incomplete: the unreadable file may be the missing document.
        return Decision(
            outcome=DecisionOutcome.REJECT,
            decided_by=DecidedBy.SYSTEM,
            reason_codes=[RuleId.INCOMPLETE_PACKAGE],
            rationale=completeness_flag.details,
        )

    doc_expired = [f for f in validation_flags if f.rule_id == RuleId.DOC_EXPIRED]
    if doc_expired and not escalation_triggers and not degraded:
        return Decision(
            outcome=DecisionOutcome.REJECT,
            decided_by=DecidedBy.SYSTEM,
            reason_codes=[RuleId.DOCUMENT_EXPIRED],
            rationale=doc_expired[0].details,
        )

    if escalation_triggers or degraded:
        codes = sorted({f.rule_id for f in escalation_triggers})
        if degraded:
            codes.append(RuleId.DEGRADED_TO_MANUAL)
        return Decision(
            outcome=DecisionOutcome.ESCALATE,
            decided_by=DecidedBy.SYSTEM,
            reason_codes=codes,
            rationale="mandatory human review triggered",
        )

    return Decision(
        outcome=DecisionOutcome.APPROVE,
        decided_by=DecidedBy.SYSTEM,
        reason_codes=[RuleId.ALL_CHECKS_PASSED],
        rationale="all validation checks passed, no escalation triggers fired",
    )
