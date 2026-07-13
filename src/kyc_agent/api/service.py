"""Case orchestration service used by the API routes.

Wraps the compiled graph: starts background runs, reads checkpointed
state, resumes interrupted cases. All durable state lives in the
checkpointer, so any instance can serve any case after a restart; the
in-process task map only tracks runs started by this instance.
"""

import asyncio
from typing import Any
from uuid import uuid4

import structlog
from langgraph.errors import GraphRecursionError
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from kyc_agent.api.schemas import CaseStatusResponse
from kyc_agent.graph.context import PipelineContext
from kyc_agent.graph.state import KYCState
from kyc_agent.schemas.case import CaseStatus, KYCPackage
from kyc_agent.schemas.decisions import HumanDecision

logger = structlog.get_logger(__name__)


class CaseNotFoundError(LookupError):
    pass


class CaseNotAwaitingReviewError(RuntimeError):
    pass


class CaseService:
    def __init__(
        self,
        graph: CompiledStateGraph[KYCState, PipelineContext],
        context: PipelineContext,
    ) -> None:
        self._graph = graph
        self._context = context
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def _config(self, case_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": case_id},
            "recursion_limit": self._context.settings.graph_recursion_limit,
        }

    def submit(self, package: KYCPackage) -> str:
        case_id = uuid4().hex
        task = asyncio.create_task(self._run(case_id, package), name=f"kyc-case-{case_id}")
        self._tasks[case_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(case_id, None))
        return case_id

    async def _run(self, case_id: str, package: KYCPackage) -> None:
        try:
            await self._graph.ainvoke(
                {"case_id": case_id, "package": package, "status": CaseStatus.RECEIVED},
                config=self._config(case_id),
                context=self._context,
            )
        except GraphRecursionError:
            # Bounded execution tripped: surface it, never loop forever.
            logger.error("recursion_limit_exceeded", case_id=case_id)
        except Exception:
            logger.exception("case_run_failed", case_id=case_id)

    async def status(self, case_id: str) -> CaseStatusResponse:
        snapshot = await self._graph.aget_state(self._config(case_id))
        values: KYCState = snapshot.values  # type: ignore[assignment]
        if not values:
            if case_id in self._tasks:
                return CaseStatusResponse(case_id=case_id, status=CaseStatus.RECEIVED)
            raise CaseNotFoundError(case_id)

        interrupts = [i for task in snapshot.tasks for i in task.interrupts]
        validation = values.get("validation")
        risk = values.get("risk")
        return CaseStatusResponse(
            case_id=case_id,
            status=values.get("status", CaseStatus.RECEIVED),
            decision=values.get("decision"),
            risk_level=risk.level if risk else None,
            overall_confidence=validation.overall_confidence if validation else None,
            degraded=bool(values.get("degraded_reasons")),
            degraded_reasons=values.get("degraded_reasons", []),
            review_request=interrupts[0].value if interrupts else None,
        )

    async def review(self, case_id: str, decision: HumanDecision) -> CaseStatusResponse:
        snapshot = await self._graph.aget_state(self._config(case_id))
        if not snapshot.values:
            raise CaseNotFoundError(case_id)
        if not any(task.interrupts for task in snapshot.tasks):
            raise CaseNotAwaitingReviewError(case_id)

        await self._graph.ainvoke(
            Command(resume=decision.model_dump()),
            config=self._config(case_id),
            context=self._context,
        )
        return await self.status(case_id)

    async def exists(self, case_id: str) -> bool:
        snapshot = await self._graph.aget_state(self._config(case_id))
        return bool(snapshot.values) or case_id in self._tasks

    async def is_finished(self, case_id: str) -> bool:
        response = await self.status(case_id)
        return response.status in (CaseStatus.COMPLETED, CaseStatus.FAILED)
