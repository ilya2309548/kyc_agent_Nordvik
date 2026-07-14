"""FastAPI application (SPEC 10).

App factory pattern: ``create_app`` accepts explicit settings so tests
can run against the in-memory backend; the module-level ``app`` used by
uvicorn reads settings from the environment.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request
from langgraph.checkpoint.memory import MemorySaver
from sse_starlette.sse import EventSourceResponse

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

from kyc_agent.api.schemas import (
    AuditEventResponse,
    CaseStatusResponse,
    HealthResponse,
    SubmitCaseResponse,
)
from kyc_agent.api.service import (
    CaseNotAwaitingReviewError,
    CaseNotFoundError,
    CaseService,
)
from kyc_agent.audit.sink import AuditSink, InMemoryAuditSink
from kyc_agent.config import Settings, get_settings
from kyc_agent.graph import build_context, build_graph
from kyc_agent.observability import configure_logging
from kyc_agent.persistence import PostgresAuditSink, create_pool, setup_postgres
from kyc_agent.schemas.case import CaseStatus, KYCPackage
from kyc_agent.schemas.decisions import HumanDecision

_SSE_POLL_SECONDS = 0.5


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings if settings is not None else get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        pool = None
        audit: AuditSink
        checkpointer: "BaseCheckpointSaver[Any]"
        if app_settings.persistence_backend == "postgres":
            pool = create_pool(app_settings.database_url)
            checkpointer = await setup_postgres(pool)
            audit = PostgresAuditSink(pool)
        else:
            checkpointer = MemorySaver()
            audit = InMemoryAuditSink()

        context = build_context(app_settings, audit=audit)
        graph = build_graph(checkpointer)

        app.state.settings = app_settings
        app.state.pool = pool
        app.state.audit = context.audit
        app.state.service = CaseService(graph, context)
        try:
            yield
        finally:
            if pool is not None:
                await pool.close()

    app = FastAPI(
        title="Nordvik KYC Processing API",
        version="0.1.0",
        description="Multi-agent KYC document processing pipeline (LangGraph)",
        lifespan=lifespan,
    )

    def service(request: Request) -> CaseService:
        return request.app.state.service  # type: ignore[no-any-return]

    @app.post("/api/v1/cases", response_model=SubmitCaseResponse, status_code=202)
    async def submit_case(package: KYCPackage, request: Request) -> SubmitCaseResponse:
        case_id = service(request).submit(package)
        return SubmitCaseResponse(case_id=case_id, status=CaseStatus.RECEIVED)

    @app.get("/api/v1/cases/{case_id}", response_model=CaseStatusResponse)
    async def case_status(case_id: str, request: Request) -> CaseStatusResponse:
        try:
            return await service(request).status(case_id)
        except CaseNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"case {case_id} not found") from exc

    @app.post("/api/v1/cases/{case_id}/review", response_model=CaseStatusResponse)
    async def review_case(
        case_id: str, decision: HumanDecision, request: Request
    ) -> CaseStatusResponse:
        try:
            return await service(request).review(case_id, decision)
        except CaseNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"case {case_id} not found") from exc
        except CaseNotAwaitingReviewError as exc:
            raise HTTPException(
                status_code=409, detail=f"case {case_id} is not awaiting human review"
            ) from exc

    @app.get("/api/v1/cases/{case_id}/audit", response_model=list[AuditEventResponse])
    async def case_audit(case_id: str, request: Request) -> list[AuditEventResponse]:
        if not await service(request).exists(case_id):
            raise HTTPException(status_code=404, detail=f"case {case_id} not found")
        events = await request.app.state.audit.events(case_id)
        return [
            AuditEventResponse(ts=e.ts, node=e.node, event_type=e.event_type, payload=e.payload)
            for e in events
        ]

    @app.get("/api/v1/cases/{case_id}/events")
    async def case_events(case_id: str, request: Request) -> EventSourceResponse:
        """SSE stream of audit events until the case reaches a final state."""
        if not await service(request).exists(case_id):
            raise HTTPException(status_code=404, detail=f"case {case_id} not found")

        async def stream() -> AsyncIterator[dict[str, Any]]:
            sent = 0
            while True:
                events = await request.app.state.audit.events(case_id)
                for event in events[sent:]:
                    yield {
                        "event": event.event_type,
                        "data": AuditEventResponse(
                            ts=event.ts,
                            node=event.node,
                            event_type=event.event_type,
                            payload=event.payload,
                        ).model_dump_json(),
                    }
                sent = len(events)
                if await service(request).is_finished(case_id) and sent == len(
                    await request.app.state.audit.events(case_id)
                ):
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(_SSE_POLL_SECONDS)

        return EventSourceResponse(stream())

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        pool = request.app.state.pool
        if pool is None:
            return HealthResponse(status="ok", database="memory")
        try:
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
        except Exception as exc:
            raise HTTPException(status_code=503, detail="database unavailable") from exc
        return HealthResponse(status="ok", database="postgres")

    return app


app = create_app()
