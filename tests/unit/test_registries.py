"""Unit tests for mock sanctions/PEP registries (SPEC 12.2)."""

import pytest

from kyc_agent.tools.registries import (
    RegistryUnavailableError,
    build_pep_client,
    build_sanctions_client,
)


class TestSanctionsRegistry:
    async def test_exact_match(self) -> None:
        hits = await build_sanctions_client().search("Viktor Salo")
        assert len(hits) == 1
        assert hits[0].registry == "sanctions"
        assert hits[0].matched_name == "Viktor Salo"
        assert hits[0].list_entry == "EU-2024-0113"
        assert hits[0].score == 1.0

    async def test_fuzzy_match_case_and_spacing(self) -> None:
        hits = await build_sanctions_client().search("  viktor   SALO ")
        assert len(hits) == 1

    async def test_near_miss_spelling_still_matches(self) -> None:
        hits = await build_sanctions_client().search("Viktor Sallo")
        assert len(hits) == 1
        assert hits[0].score < 1.0

    async def test_clean_name_has_no_hits(self) -> None:
        assert await build_sanctions_client().search("Anna Virtanen") == []


class TestPepRegistry:
    async def test_pep_match(self) -> None:
        hits = await build_pep_client().search("Maarika Kask")
        assert len(hits) == 1
        assert hits[0].registry == "pep"

    async def test_registries_are_independent(self) -> None:
        assert await build_pep_client().search("Viktor Salo") == []


class TestFailureInjection:
    async def test_full_failure_rate_always_raises(self) -> None:
        client = build_sanctions_client(failure_rate=1.0)
        with pytest.raises(RegistryUnavailableError):
            await client.search("Anna Virtanen")

    async def test_zero_failure_rate_never_raises(self) -> None:
        client = build_sanctions_client(failure_rate=0.0)
        for _ in range(20):
            await client.search("Anna Virtanen")

    async def test_failure_sequence_is_reproducible(self) -> None:
        async def outcomes(seed: int) -> list[bool]:
            client = build_sanctions_client(failure_rate=0.5, seed=seed)
            result = []
            for _ in range(10):
                try:
                    await client.search("x")
                    result.append(True)
                except RegistryUnavailableError:
                    result.append(False)
            return result

        assert await outcomes(seed=7) == await outcomes(seed=7)
