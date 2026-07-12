"""Bounded retry with model fallback (SPEC 4.7).

Budget per step: the primary implementation gets ``1 + max_retries``
attempts, the fallback implementation gets one final attempt. When the
budget is exhausted a StepExhaustedError carries the full error history
so the caller can degrade the case to manual review instead of failing
silently.
"""

from collections.abc import Awaitable, Callable

import structlog

from kyc_agent.llm.base import PipelineStep
from kyc_agent.schemas.decisions import ProcessingError

logger = structlog.get_logger(__name__)


class StepExhaustedError(RuntimeError):
    def __init__(self, step: PipelineStep, errors: list[ProcessingError]) -> None:
        super().__init__(f"step {step} exhausted retry budget after {len(errors)} attempts")
        self.step = step
        self.errors = errors


async def run_resilient[T](
    step: PipelineStep,
    node: str,
    primary: Callable[[], Awaitable[T]],
    fallback: Callable[[], Awaitable[T]] | None,
    max_retries: int,
) -> tuple[T, list[ProcessingError]]:
    """Run ``primary`` with retries, then ``fallback`` once.

    Returns the result plus every error seen on the way (for the audit
    trail); raises StepExhaustedError when nothing succeeded.
    """
    errors: list[ProcessingError] = []
    attempt = 0

    for _ in range(1 + max_retries):
        attempt += 1
        try:
            return await primary(), errors
        except Exception as exc:  # noqa: BLE001 — every failure type must degrade, not crash
            errors.append(
                ProcessingError(
                    node=node,
                    error_type=type(exc).__name__,
                    message=str(exc),
                    attempt=attempt,
                )
            )
            logger.warning(
                "step_attempt_failed", step=step, node=node, attempt=attempt, error=str(exc)
            )

    if fallback is not None:
        attempt += 1
        try:
            return await fallback(), errors
        except Exception as exc:  # noqa: BLE001
            errors.append(
                ProcessingError(
                    node=node,
                    error_type=type(exc).__name__,
                    message=f"fallback failed: {exc}",
                    attempt=attempt,
                )
            )
            logger.warning(
                "step_fallback_failed", step=step, node=node, attempt=attempt, error=str(exc)
            )

    raise StepExhaustedError(step, errors)
