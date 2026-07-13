"""Shared fixtures: golden-set loading and graph construction helpers."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

from kyc_agent.audit.sink import InMemoryAuditSink
from kyc_agent.config import Settings
from kyc_agent.graph import build_context, build_graph
from kyc_agent.graph.context import PipelineContext
from kyc_agent.schemas import KYCPackage

GOLDEN_SET_PATH = Path(__file__).resolve().parent.parent / "data" / "synthetic" / "golden_set.json"

# The golden set is frozen relative to this date (document expiries etc.).
REFERENCE_DATE = date(2026, 7, 13)


@pytest.fixture(scope="session")
def golden_cases() -> dict[str, dict[str, Any]]:
    raw = json.loads(GOLDEN_SET_PATH.read_text())
    return {c["case_id"]: c for c in raw["cases"]}


@pytest.fixture
def settings() -> Settings:
    # Explicit values: tests must not depend on the developer's .env.
    return Settings(_env_file=None)


@pytest.fixture
def audit_sink() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def context(settings: Settings, audit_sink: InMemoryAuditSink) -> PipelineContext:
    return build_context(settings, audit=audit_sink, today=REFERENCE_DATE)


@pytest.fixture
def graph():  # noqa: ANN201 — CompiledStateGraph generics are unwieldy here
    return build_graph(MemorySaver())


def package_from_case(case: dict[str, Any]) -> KYCPackage:
    return KYCPackage.model_validate(case["package"])


def graph_config(case_id: str, recursion_limit: int = 25) -> dict[str, Any]:
    return {"configurable": {"thread_id": case_id}, "recursion_limit": recursion_limit}
