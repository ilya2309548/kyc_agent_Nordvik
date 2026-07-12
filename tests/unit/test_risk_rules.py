"""Unit tests for risk rules and the decision matrix (SPEC 7.3, 7.4)."""

from decimal import Decimal

from kyc_agent.rules.ids import RuleId
from kyc_agent.rules.risk import (
    RiskThresholds,
    decide_gate,
    evaluate_risk_rules,
    risk_level,
)
from kyc_agent.schemas.case import CustomerType
from kyc_agent.schemas.decisions import (
    DecidedBy,
    DecisionOutcome,
    RegistryHit,
    RiskLevel,
    RuleFlag,
    Severity,
)


def evaluate(**overrides: object) -> list[RuleFlag]:
    kwargs: dict[str, object] = {
        "customer_type": CustomerType.INDIVIDUAL,
        "expected_monthly_volume_eur": Decimal(2500),
        "overall_confidence": 0.95,
        "validation_flags": [],
        "sanctions_hits": [],
        "pep_hits": [],
        "ubo_hits": [],
        "registry_unavailable": [],
    }
    kwargs.update(overrides)
    return evaluate_risk_rules(**kwargs)  # type: ignore[arg-type]


def sanctions_hit() -> RegistryHit:
    return RegistryHit(
        registry="sanctions", matched_name="Viktor Salo", score=0.97, list_entry="EU-2024-0113"
    )


class TestEscalationTriggers:
    def test_clean_case_has_no_triggers(self) -> None:
        assert evaluate() == []

    def test_sanctions_hit_triggers(self) -> None:
        triggered = evaluate(sanctions_hits=[sanctions_hit()])
        assert [f.rule_id for f in triggered] == [RuleId.SANCTIONS_HIT]

    def test_pep_hit_triggers(self) -> None:
        hit = RegistryHit(
            registry="pep", matched_name="Maarika Kask", score=0.9, list_entry="PEP-EE-77"
        )
        triggered = evaluate(pep_hits=[hit])
        assert [f.rule_id for f in triggered] == [RuleId.PEP_MATCH]

    def test_ubo_hit_triggers(self) -> None:
        triggered = evaluate(ubo_hits=[sanctions_hit()])
        assert [f.rule_id for f in triggered] == [RuleId.UBO_SANCTIONS_OR_PEP]

    def test_high_volume_individual_threshold(self) -> None:
        assert evaluate(expected_monthly_volume_eur=Decimal(10_000)) == []
        triggered = evaluate(expected_monthly_volume_eur=Decimal(10_001))
        assert [f.rule_id for f in triggered] == [RuleId.HIGH_VOLUME]

    def test_high_volume_business_threshold(self) -> None:
        assert (
            evaluate(
                customer_type=CustomerType.BUSINESS,
                expected_monthly_volume_eur=Decimal(30_000),
            )
            == []
        )
        triggered = evaluate(
            customer_type=CustomerType.BUSINESS,
            expected_monthly_volume_eur=Decimal(50_001),
        )
        assert [f.rule_id for f in triggered] == [RuleId.HIGH_VOLUME]

    def test_low_confidence_triggers(self) -> None:
        triggered = evaluate(overall_confidence=0.74)
        assert [f.rule_id for f in triggered] == [RuleId.LOW_CONFIDENCE]

    def test_critical_mismatch_from_validation(self) -> None:
        flag = RuleFlag(rule_id=RuleId.NAME_MISMATCH, severity=Severity.CRITICAL, details="x")
        triggered = evaluate(validation_flags=[flag])
        assert [f.rule_id for f in triggered] == [RuleId.CRITICAL_MISMATCH]

    def test_warning_mismatch_does_not_trigger(self) -> None:
        flag = RuleFlag(rule_id=RuleId.NAME_MISMATCH, severity=Severity.WARNING, details="x")
        assert evaluate(validation_flags=[flag]) == []

    def test_doc_expired_is_not_a_critical_mismatch_source(self) -> None:
        flag = RuleFlag(rule_id=RuleId.DOC_EXPIRED, severity=Severity.CRITICAL, details="x")
        assert evaluate(validation_flags=[flag]) == []

    def test_registry_unavailable_triggers(self) -> None:
        triggered = evaluate(registry_unavailable=["sanctions"])
        assert [f.rule_id for f in triggered] == [RuleId.REGISTRY_UNAVAILABLE]

    def test_custom_thresholds(self) -> None:
        strict = RiskThresholds(confidence=0.99)
        triggered = evaluate_risk_rules(
            customer_type=CustomerType.INDIVIDUAL,
            expected_monthly_volume_eur=Decimal(100),
            overall_confidence=0.98,
            validation_flags=[],
            sanctions_hits=[],
            pep_hits=[],
            ubo_hits=[],
            registry_unavailable=[],
            thresholds=strict,
        )
        assert [f.rule_id for f in triggered] == [RuleId.LOW_CONFIDENCE]


class TestRiskLevel:
    def test_levels(self) -> None:
        assert risk_level([]) is RiskLevel.LOW
        volume = RuleFlag(rule_id=RuleId.HIGH_VOLUME, severity=Severity.CRITICAL, details="x")
        assert risk_level([volume]) is RiskLevel.MEDIUM
        sanc = RuleFlag(rule_id=RuleId.SANCTIONS_HIT, severity=Severity.CRITICAL, details="x")
        assert risk_level([volume, sanc]) is RiskLevel.HIGH


class TestDecisionMatrix:
    def test_incomplete_package_auto_rejects(self) -> None:
        flag = RuleFlag(rule_id=RuleId.INCOMPLETE_PACKAGE, severity=Severity.CRITICAL, details="x")
        decision = decide_gate(flag, [], [], degraded=False)
        assert decision.outcome is DecisionOutcome.REJECT
        assert decision.decided_by is DecidedBy.SYSTEM
        assert decision.reason_codes == [RuleId.INCOMPLETE_PACKAGE]

    def test_expired_document_auto_rejects_when_otherwise_clean(self) -> None:
        expired = RuleFlag(rule_id=RuleId.DOC_EXPIRED, severity=Severity.CRITICAL, details="x")
        decision = decide_gate(None, [expired], [], degraded=False)
        assert decision.outcome is DecisionOutcome.REJECT
        assert decision.reason_codes == [RuleId.DOCUMENT_EXPIRED]

    def test_expired_document_with_trigger_escalates(self) -> None:
        # Escalation invariant beats the auto-reject shortcut.
        expired = RuleFlag(rule_id=RuleId.DOC_EXPIRED, severity=Severity.CRITICAL, details="x")
        sanc = RuleFlag(rule_id=RuleId.SANCTIONS_HIT, severity=Severity.CRITICAL, details="x")
        decision = decide_gate(None, [expired], [sanc], degraded=False)
        assert decision.outcome is DecisionOutcome.ESCALATE

    def test_any_trigger_escalates(self) -> None:
        sanc = RuleFlag(rule_id=RuleId.SANCTIONS_HIT, severity=Severity.CRITICAL, details="x")
        decision = decide_gate(None, [], [sanc], degraded=False)
        assert decision.outcome is DecisionOutcome.ESCALATE
        assert RuleId.SANCTIONS_HIT in decision.reason_codes

    def test_degraded_case_escalates(self) -> None:
        decision = decide_gate(None, [], [], degraded=True)
        assert decision.outcome is DecisionOutcome.ESCALATE
        assert RuleId.DEGRADED_TO_MANUAL in decision.reason_codes

    def test_clean_case_auto_approves(self) -> None:
        decision = decide_gate(None, [], [], degraded=False)
        assert decision.outcome is DecisionOutcome.APPROVE
        assert decision.reason_codes == [RuleId.ALL_CHECKS_PASSED]

    def test_escalation_recall_invariant(self) -> None:
        """No combination of inputs with a fired trigger may auto-approve."""
        trigger = RuleFlag(rule_id=RuleId.LOW_CONFIDENCE, severity=Severity.CRITICAL, details="x")
        expired = RuleFlag(rule_id=RuleId.DOC_EXPIRED, severity=Severity.CRITICAL, details="x")
        for validation_flags in ([], [expired]):
            for degraded in (False, True):
                decision = decide_gate(None, validation_flags, [trigger], degraded)
                assert decision.outcome is DecisionOutcome.ESCALATE
