"""Eval harness (SPEC 11): metrics over the synthetic golden set.

Runs every golden case through the real graph (fake provider by default;
set MODEL_* env vars to evaluate live models with the same command) and
computes:

- field_accuracy        — extraction correctness against golden values
- auto_rate             — share of typical cases decided without a human
- escalation_recall     — MUST be 1.0 (hard invariant, exit code 1 if not)
- escalation_precision  — escalations that were actually warranted
- decision_accuracy     — final outcome vs expected outcome

``--no-validator`` disables the agent-checks-agent grounding step
(ablation); by default both configurations run and land in
eval/results.json so the validator's contribution stays measurable.

Usage:  uv run python eval/run_eval.py [--no-validator] [--quiet]
"""

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from kyc_agent.config import Settings
from kyc_agent.graph import build_context, build_graph
from kyc_agent.observability import configure_logging
from kyc_agent.schemas import KYCPackage

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_PATH = ROOT / "data" / "synthetic" / "golden_set.json"
RESULTS_PATH = ROOT / "eval" / "results.json"


def normalize(value: Any) -> str:
    return " ".join(str(value).casefold().split())


async def run_single_case(
    case: dict[str, Any], settings: Settings, reference_date: date, evaluator_enabled: bool
) -> dict[str, Any]:
    context = build_context(settings, today=reference_date, evaluator_enabled=evaluator_enabled)
    graph = build_graph(MemorySaver())
    result = await graph.ainvoke(
        {"case_id": case["case_id"], "package": KYCPackage.model_validate(case["package"])},
        config={
            "configurable": {"thread_id": case["case_id"]},
            "recursion_limit": settings.graph_recursion_limit,
        },
        context=context,
    )

    escalated = "__interrupt__" in result
    decision = result.get("decision")
    predicted_outcome = "escalate" if escalated else (decision.outcome if decision else "unknown")

    field_total = 0
    field_correct = 0
    extractions = {e.document_id: e for e in result.get("extractions", [])}
    for doc_id, golden_fields in case["expected"].get("fields", {}).items():
        extraction = extractions.get(doc_id)
        for field, golden_value in golden_fields.items():
            field_total += 1
            if extraction is None or extraction.extraction_error is not None:
                continue
            actual = extraction.fields.get(field)
            if actual is not None and normalize(actual) == normalize(golden_value):
                field_correct += 1

    reason_codes = list(decision.reason_codes) if decision else []
    expected = case["expected"]
    return {
        "case_id": case["case_id"],
        "typical": case["typical"],
        "expected_outcome": expected["outcome"],
        "expected_escalation": expected["escalation"],
        "predicted_outcome": str(predicted_outcome),
        "predicted_escalation": escalated,
        "reason_codes": reason_codes,
        "reason_codes_ok": set(expected["reason_codes_include"]) <= set(reason_codes),
        "outcome_ok": str(predicted_outcome) == expected["outcome"],
        "field_total": field_total,
        "field_correct": field_correct,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    field_total = sum(r["field_total"] for r in rows)
    field_correct = sum(r["field_correct"] for r in rows)

    typical = [r for r in rows if r["typical"]]
    auto = [r for r in typical if not r["predicted_escalation"]]

    should_escalate = [r for r in rows if r["expected_escalation"]]
    did_escalate = [r for r in rows if r["predicted_escalation"]]
    true_escalations = [r for r in did_escalate if r["expected_escalation"]]

    return {
        "cases": len(rows),
        "field_accuracy": round(field_correct / field_total, 4) if field_total else None,
        "auto_rate_typical": round(len(auto) / len(typical), 4) if typical else None,
        "escalation_recall": (
            round(len(true_escalations) / len(should_escalate), 4) if should_escalate else None
        ),
        "escalation_precision": (
            round(len(true_escalations) / len(did_escalate), 4) if did_escalate else None
        ),
        "decision_accuracy": round(sum(1 for r in rows if r["outcome_ok"]) / len(rows), 4),
        "reason_codes_accuracy": round(sum(1 for r in rows if r["reason_codes_ok"]) / len(rows), 4),
        "false_auto_approvals": sum(
            1
            for r in rows
            if r["expected_escalation"]
            and not r["predicted_escalation"]
            and r["predicted_outcome"] == "approve"
        ),
    }


async def run_config(
    cases: list[dict[str, Any]], reference_date: date, evaluator_enabled: bool
) -> dict[str, Any]:
    settings = Settings(_env_file=str(ROOT / ".env") if (ROOT / ".env").exists() else None)
    rows = [
        await run_single_case(case, settings, reference_date, evaluator_enabled) for case in cases
    ]
    return {"summary": summarize(rows), "cases": rows}


def print_summary(name: str, summary: dict[str, Any]) -> None:
    print(f"\n=== {name} ===")
    for key, value in summary.items():
        print(f"{key:24s} {value}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-validator",
        action="store_true",
        help="run only the ablation (grounding evaluator disabled)",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress trajectory logs")
    args = parser.parse_args()

    configure_logging(level=30 if args.quiet else 20)

    golden = json.loads(GOLDEN_PATH.read_text())
    cases = golden["cases"]
    reference_date = date.fromisoformat(golden["reference_date"])

    results: dict[str, Any] = {"golden_set": str(GOLDEN_PATH.name), "runs": {}}

    if not args.no_validator:
        results["runs"]["full_pipeline"] = await run_config(cases, reference_date, True)
        print_summary("full pipeline", results["runs"]["full_pipeline"]["summary"])

    results["runs"]["no_validator_ablation"] = await run_config(cases, reference_date, False)
    print_summary("no-validator ablation", results["runs"]["no_validator_ablation"]["summary"])

    RESULTS_PATH.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\nresults written to {RESULTS_PATH}")

    # Hard invariant (SPEC 11): the full pipeline must never miss an escalation.
    if not args.no_validator:
        recall = results["runs"]["full_pipeline"]["summary"]["escalation_recall"]
        if recall != 1.0:
            print(f"FAIL: escalation_recall={recall} (must be 1.0)", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
