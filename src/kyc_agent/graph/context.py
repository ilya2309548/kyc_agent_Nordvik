"""Runtime context injected into graph nodes (not checkpointed).

Carries the step services, registries, audit sink and settings. Built
once per process; tests and the eval harness construct their own to swap
sinks, clocks and failure injection.
"""

from dataclasses import dataclass, field
from datetime import date

from kyc_agent.audit.sink import AuditSink, InMemoryAuditSink, SafeAuditSink
from kyc_agent.config import Settings
from kyc_agent.llm.base import PipelineServices
from kyc_agent.llm.factory import build_fallback_services, build_services
from kyc_agent.tools.registries import (
    MockRegistryClient,
    build_pep_client,
    build_sanctions_client,
)


@dataclass
class PipelineContext:
    settings: Settings
    services: PipelineServices
    fallback: PipelineServices
    audit: AuditSink
    sanctions: MockRegistryClient
    pep: MockRegistryClient
    # Injectable clock so expiry checks are reproducible in tests/eval.
    today: date = field(default_factory=date.today)
    # Eval ablation (SPEC 11): disables only the agent-checks-agent
    # grounding step, never the deterministic rules.
    evaluator_enabled: bool = True


def build_context(
    settings: Settings,
    audit: AuditSink | None = None,
    today: date | None = None,
    evaluator_enabled: bool = True,
) -> PipelineContext:
    return PipelineContext(
        settings=settings,
        services=build_services(settings),
        fallback=build_fallback_services(settings),
        audit=SafeAuditSink(audit if audit is not None else InMemoryAuditSink()),
        sanctions=build_sanctions_client(failure_rate=settings.registry_failure_rate),
        pep=build_pep_client(failure_rate=settings.registry_failure_rate),
        today=today if today is not None else date.today(),
        evaluator_enabled=evaluator_enabled,
    )
