"""Audit trail sinks (SPEC 4.8, 6.5).

Every graph node emits domain events through an AuditSink. A sink failure
must never crash the pipeline — SafeAuditSink downgrades write errors to
logs — but the decision trail itself is written before a case finalizes.
"""

from datetime import UTC, datetime
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class AuditEvent(BaseModel):
    case_id: str
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    node: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class AuditSink(Protocol):
    async def emit(self, event: AuditEvent) -> None: ...

    async def events(self, case_id: str) -> list[AuditEvent]: ...


class InMemoryAuditSink:
    """Test/offline sink; keeps events in process memory."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    async def emit(self, event: AuditEvent) -> None:
        self._events.append(event)

    async def events(self, case_id: str) -> list[AuditEvent]:
        return [e for e in self._events if e.case_id == case_id]


class SafeAuditSink:
    """Decorator sink: write failures are logged, never raised (SPEC 6.5)."""

    def __init__(self, inner: AuditSink) -> None:
        self._inner = inner

    async def emit(self, event: AuditEvent) -> None:
        try:
            await self._inner.emit(event)
        except Exception as exc:  # noqa: BLE001 — audit outage must not kill the case
            logger.error(
                "audit_write_failed",
                case_id=event.case_id,
                node=event.node,
                event_type=event.event_type,
                error=str(exc),
            )

    async def events(self, case_id: str) -> list[AuditEvent]:
        return await self._inner.events(case_id)


async def emit(
    sink: AuditSink,
    case_id: str,
    node: str,
    event_type: str,
    **payload: Any,
) -> None:
    """Convenience wrapper used by graph nodes."""
    await sink.emit(AuditEvent(case_id=case_id, node=node, event_type=event_type, payload=payload))
