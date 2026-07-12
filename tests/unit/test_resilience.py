"""Unit tests for bounded retry + fallback (SPEC 4.7)."""

import pytest

from kyc_agent.llm.base import PipelineStep
from kyc_agent.llm.resilience import StepExhaustedError, run_resilient


class Flaky:
    """Callable failing a fixed number of times before succeeding."""

    def __init__(self, failures: int, result: str = "ok") -> None:
        self.failures = failures
        self.calls = 0
        self.result = result

    async def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError(f"boom #{self.calls}")
        return self.result


class TestRunResilient:
    async def test_success_first_try(self) -> None:
        primary = Flaky(failures=0)
        result, errors = await run_resilient(
            PipelineStep.EXTRACTOR, "extract", primary, None, max_retries=2
        )
        assert result == "ok"
        assert errors == []
        assert primary.calls == 1

    async def test_retry_then_success(self) -> None:
        primary = Flaky(failures=2)
        result, errors = await run_resilient(
            PipelineStep.EXTRACTOR, "extract", primary, None, max_retries=2
        )
        assert result == "ok"
        assert len(errors) == 2
        assert [e.attempt for e in errors] == [1, 2]

    async def test_fallback_used_after_primary_exhausted(self) -> None:
        primary = Flaky(failures=99)
        fallback = Flaky(failures=0, result="from-fallback")
        result, errors = await run_resilient(
            PipelineStep.EXTRACTOR, "extract", primary, fallback, max_retries=2
        )
        assert result == "from-fallback"
        assert primary.calls == 3  # 1 + max_retries
        assert fallback.calls == 1
        assert len(errors) == 3

    async def test_exhaustion_raises_with_error_history(self) -> None:
        primary = Flaky(failures=99)
        fallback = Flaky(failures=99)
        with pytest.raises(StepExhaustedError) as exc_info:
            await run_resilient(
                PipelineStep.VALIDATOR, "validate", primary, fallback, max_retries=1
            )
        err = exc_info.value
        assert err.step is PipelineStep.VALIDATOR
        assert len(err.errors) == 3  # 2 primary + 1 fallback
        assert "fallback failed" in err.errors[-1].message
        assert all(e.node == "validate" for e in err.errors)

    async def test_no_fallback_exhaustion(self) -> None:
        with pytest.raises(StepExhaustedError):
            await run_resilient(
                PipelineStep.ROUTER, "route", Flaky(failures=99), None, max_retries=0
            )
