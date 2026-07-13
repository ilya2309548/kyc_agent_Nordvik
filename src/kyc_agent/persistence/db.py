"""PostgreSQL persistence: checkpointer pool and audit trail storage
(SPEC 4.6, 6.5).

One connection pool serves both the LangGraph AsyncPostgresSaver and the
audit sink. ``setup_postgres`` is idempotent and safe to run on every
application start.
"""

import json
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

from kyc_agent.audit.sink import AuditEvent

AUDIT_DDL = (
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        id          BIGSERIAL PRIMARY KEY,
        case_id     TEXT NOT NULL,
        ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
        node        TEXT NOT NULL,
        event_type  TEXT NOT NULL,
        payload     JSONB NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_case ON audit_events (case_id, ts)",
)


def create_pool(database_url: str, max_size: int = 10) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        conninfo=database_url,
        max_size=max_size,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )


async def setup_postgres(pool: AsyncConnectionPool) -> AsyncPostgresSaver:
    """Open the pool, run idempotent migrations, return a ready checkpointer."""
    await pool.open()
    checkpointer = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
    await checkpointer.setup()
    async with pool.connection() as conn:
        for statement in AUDIT_DDL:
            await conn.execute(statement)
    return checkpointer


class PostgresAuditSink:
    """Audit trail in the audit_events table (SPEC 6.5)."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def emit(self, event: AuditEvent) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO audit_events (case_id, ts, node, event_type, payload) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    event.case_id,
                    event.ts,
                    event.node,
                    event.event_type,
                    Json(event.payload, dumps=lambda o: json.dumps(o, default=str)),
                ),
            )

    async def events(self, case_id: str) -> list[AuditEvent]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT case_id, ts, node, event_type, payload FROM audit_events "
                "WHERE case_id = %s ORDER BY ts, id",
                (case_id,),
            )
            rows: list[dict[str, Any]] = await cursor.fetchall()
        return [AuditEvent.model_validate(row) for row in rows]
