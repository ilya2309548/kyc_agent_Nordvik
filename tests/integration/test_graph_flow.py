"""Integration tests: full graph runs over the synthetic golden cases.

Everything runs with the fake provider and MemorySaver, which exercises
the exact same graph path as production (SPEC 9).
"""

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from kyc_agent.graph import build_graph
from kyc_agent.rules.ids import RuleId
from kyc_agent.schemas import CaseStatus, DecidedBy, DecisionOutcome
from tests.conftest import graph_config, package_from_case


async def run_case(graph: Any, context: Any, case: dict[str, Any]) -> dict[str, Any]:
    case_id = case["case_id"]
    return await graph.ainvoke(
        {"case_id": case_id, "package": package_from_case(case)},
        config=graph_config(case_id),
        context=context,
    )


def interrupt_payload(result: dict[str, Any]) -> dict[str, Any]:
    assert "__interrupt__" in result, "expected the graph to pause on interrupt"
    return result["__interrupt__"][0].value


class TestAutoDecisions:
    async def test_clean_case_auto_approves(self, graph, context, audit_sink, golden_cases) -> None:
        result = await run_case(graph, context, golden_cases["clean-individual-1"])

        assert result["status"] == CaseStatus.COMPLETED
        decision = result["decision"]
        assert decision.outcome is DecisionOutcome.APPROVE
        assert decision.decided_by is DecidedBy.SYSTEM
        assert decision.reason_codes == [RuleId.ALL_CHECKS_PASSED]
        assert result["validation"].overall_confidence >= 0.75

        events = await audit_sink.events("clean-individual-1")
        event_types = [e.event_type for e in events]
        assert "decision_made" in event_types
        assert "case_completed" in event_types
        # Full trajectory is auditable: every pipeline node left a trace.
        nodes = {e.node for e in events}
        assert {
            "intake",
            "router",
            "orchestrator",
            "validator",
            "risk_scorer",
            "decision_gate",
            "auto_decision",
            "finalize",
        } <= nodes

    async def test_business_clean_auto_approves(self, graph, context, golden_cases) -> None:
        result = await run_case(graph, context, golden_cases["business-clean"])
        assert result["decision"].outcome is DecisionOutcome.APPROVE
        assert result["status"] == CaseStatus.COMPLETED

    async def test_address_warning_does_not_block_approval(
        self, graph, context, golden_cases
    ) -> None:
        result = await run_case(graph, context, golden_cases["address-mismatch-warning"])
        assert result["decision"].outcome is DecisionOutcome.APPROVE
        flags = [f.rule_id for f in result["validation"].rule_flags]
        assert RuleId.ADDRESS_MISMATCH in flags

    async def test_expired_document_auto_rejects(self, graph, context, golden_cases) -> None:
        result = await run_case(graph, context, golden_cases["expired-document"])
        decision = result["decision"]
        assert decision.outcome is DecisionOutcome.REJECT
        assert decision.decided_by is DecidedBy.SYSTEM
        assert decision.reason_codes == [RuleId.DOCUMENT_EXPIRED]

    async def test_incomplete_package_auto_rejects(self, graph, context, golden_cases) -> None:
        result = await run_case(graph, context, golden_cases["incomplete-package"])
        decision = result["decision"]
        assert decision.outcome is DecisionOutcome.REJECT
        assert decision.reason_codes == [RuleId.INCOMPLETE_PACKAGE]
        # No extraction happened: the orchestrator short-circuited.
        assert result.get("extractions", []) == []


class TestEscalations:
    async def test_sanctions_hit_interrupts_with_summary(
        self, graph, context, golden_cases
    ) -> None:
        result = await run_case(graph, context, golden_cases["sanctions-hit"])

        assert result["status"] == CaseStatus.AWAITING_HUMAN_REVIEW
        payload = interrupt_payload(result)
        assert RuleId.SANCTIONS_HIT in payload["reason_codes"]
        assert payload["system_recommendation"] == "reject"
        assert payload["summary"]["risk"]["level"] == "high"
        assert payload["summary"]["extracted"], "reviewer must see extracted fields"

    async def test_pep_match_escalates(self, graph, context, golden_cases) -> None:
        result = await run_case(graph, context, golden_cases["pep-match"])
        assert RuleId.PEP_MATCH in interrupt_payload(result)["reason_codes"]

    async def test_high_volume_escalates(self, graph, context, golden_cases) -> None:
        result = await run_case(graph, context, golden_cases["high-volume"])
        assert RuleId.HIGH_VOLUME in interrupt_payload(result)["reason_codes"]

    async def test_name_mismatch_escalates(self, graph, context, golden_cases) -> None:
        result = await run_case(graph, context, golden_cases["name-mismatch-critical"])
        assert RuleId.CRITICAL_MISMATCH in interrupt_payload(result)["reason_codes"]

    async def test_ubo_sanctions_escalates(self, graph, context, golden_cases) -> None:
        result = await run_case(graph, context, golden_cases["business-ubo-sanctions"])
        assert RuleId.UBO_SANCTIONS_OR_PEP in interrupt_payload(result)["reason_codes"]


class TestHumanInTheLoop:
    async def test_resume_with_human_decision_completes_case(
        self, graph, context, audit_sink, golden_cases
    ) -> None:
        case = golden_cases["sanctions-hit"]
        result = await run_case(graph, context, case)
        assert result["status"] == CaseStatus.AWAITING_HUMAN_REVIEW

        resumed = await graph.ainvoke(
            Command(resume={"outcome": "reject", "reviewer": "j.doe", "comment": "EU list match"}),
            config=graph_config(case["case_id"]),
            context=context,
        )

        assert resumed["status"] == CaseStatus.COMPLETED
        decision = resumed["decision"]
        assert decision.outcome is DecisionOutcome.REJECT
        assert decision.decided_by is DecidedBy.HUMAN
        assert decision.reviewer == "j.doe"

        events = await audit_sink.events(case["case_id"])
        human_events = [e for e in events if e.event_type == "human_decision"]
        assert len(human_events) == 1
        assert human_events[0].payload["reviewer"] == "j.doe"

    async def test_invalid_resume_payload_reinterrupts(self, graph, context, golden_cases) -> None:
        case = golden_cases["pep-match"]
        await run_case(graph, context, case)

        # Malformed review: outcome value not allowed.
        result = await graph.ainvoke(
            Command(resume={"outcome": "looks-fine", "reviewer": "j.doe"}),
            config=graph_config(case["case_id"]),
            context=context,
        )
        payload = interrupt_payload(result)
        assert "resume_error" in payload
        assert result["status"] == CaseStatus.AWAITING_HUMAN_REVIEW

        # A valid review still closes the case afterwards.
        resumed = await graph.ainvoke(
            Command(resume={"outcome": "approve", "reviewer": "j.doe", "comment": "verified"}),
            config=graph_config(case["case_id"]),
            context=context,
        )
        assert resumed["status"] == CaseStatus.COMPLETED
        assert resumed["decision"].decided_by is DecidedBy.HUMAN

    async def test_case_survives_process_restart_between_interrupt_and_resume(
        self, context, golden_cases
    ) -> None:
        case = golden_cases["sanctions-hit"]
        checkpointer = MemorySaver()

        first_graph = build_graph(checkpointer)
        result = await first_graph.ainvoke(
            {"case_id": case["case_id"], "package": package_from_case(case)},
            config=graph_config(case["case_id"]),
            context=context,
        )
        assert result["status"] == CaseStatus.AWAITING_HUMAN_REVIEW

        # "Restart": a fresh graph instance over the same checkpoint store.
        second_graph = build_graph(checkpointer)
        resumed = await second_graph.ainvoke(
            Command(resume={"outcome": "reject", "reviewer": "m.lind", "comment": "confirmed"}),
            config=graph_config(case["case_id"]),
            context=context,
        )
        assert resumed["status"] == CaseStatus.COMPLETED
        assert resumed["decision"].reviewer == "m.lind"


class TestErrorHandling:
    async def test_corrupted_document_degrades_to_human(
        self, graph, context, audit_sink, golden_cases
    ) -> None:
        case = golden_cases["corrupted-document"]
        result = await run_case(graph, context, case)

        assert result["status"] == CaseStatus.AWAITING_HUMAN_REVIEW
        payload = interrupt_payload(result)
        assert payload["degraded"] is True
        assert RuleId.DEGRADED_TO_MANUAL in payload["reason_codes"]
        assert any("unreadable" in r for r in payload["degraded_reasons"])

        # Graceful: the case is parked for a human, not failed.
        events = await audit_sink.events(case["case_id"])
        assert any(e.event_type == "rule_triggered" for e in events)

    async def test_registry_outage_forces_escalation(
        self, settings, audit_sink, golden_cases
    ) -> None:
        """'No answer' from a registry must never read as 'clean'."""
        from kyc_agent.graph import build_context

        outage_settings = settings.model_copy(update={"registry_failure_rate": 1.0})
        context = build_context(outage_settings, audit=audit_sink)
        graph = build_graph(MemorySaver())

        result = await run_case(graph, context, golden_cases["clean-individual-1"])
        payload = interrupt_payload(result)
        assert RuleId.REGISTRY_UNAVAILABLE in payload["reason_codes"]

    async def test_hallucination_caught_only_with_evaluator(self, settings, golden_cases) -> None:
        """The agent-checks-agent pattern is the only line of defense here."""
        from kyc_agent.graph import build_context
        from tests.conftest import REFERENCE_DATE

        case = golden_cases["hallucination-bait"]

        with_evaluator = build_context(settings, today=REFERENCE_DATE)
        result = await run_case(build_graph(MemorySaver()), with_evaluator, case)
        assert result["status"] == CaseStatus.AWAITING_HUMAN_REVIEW
        assert RuleId.CRITICAL_MISMATCH in interrupt_payload(result)["reason_codes"]

        without_evaluator = build_context(settings, today=REFERENCE_DATE, evaluator_enabled=False)
        result = await run_case(build_graph(MemorySaver()), without_evaluator, case)
        # False auto-approval: exactly the failure mode the evaluator prevents.
        assert result["status"] == CaseStatus.COMPLETED
        assert result["decision"].outcome is DecisionOutcome.APPROVE
