"""Deterministic, unit-testable business rules (SPEC section 7).

Everything in this package is pure: no I/O, no LLM calls, no settings
lookups — thresholds come in as arguments so the rules are trivially
testable and auditable.
"""

from kyc_agent.rules.ids import RuleId
from kyc_agent.rules.risk import decide_gate, evaluate_risk_rules, risk_level
from kyc_agent.rules.validation import MatchThresholds, run_validation_rules

__all__ = [
    "MatchThresholds",
    "RuleId",
    "decide_gate",
    "evaluate_risk_rules",
    "risk_level",
    "run_validation_rules",
]
