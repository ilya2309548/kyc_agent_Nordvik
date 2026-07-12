"""Mock sanctions and PEP registries.

Realistic external-tool interface (async call, fuzzy name search, can be
unavailable) backed by fully synthetic data. The pipeline must treat a
registry failure as a mandatory escalation, never as a clean result
(SPEC 4.7, 7.3 REGISTRY_UNAVAILABLE).
"""

import random
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

from kyc_agent.schemas.decisions import RegistryHit


class RegistryUnavailableError(RuntimeError):
    """Raised when the (mock) registry endpoint fails."""


@dataclass(frozen=True)
class RegistryEntry:
    full_name: str
    entry_id: str


# Synthetic data only — any resemblance to real persons is coincidental.
SANCTIONS_ENTRIES: tuple[RegistryEntry, ...] = (
    RegistryEntry("Viktor Salo", "EU-2024-0113"),
    RegistryEntry("Dmitri Voronov", "EU-2023-0871"),
    RegistryEntry("Halvard Eriksen", "UN-2022-0442"),
    RegistryEntry("Rustam Aliyev", "OFAC-2025-1190"),
    RegistryEntry("Meridian Shipping Ltd", "EU-2024-0555"),
)

PEP_ENTRIES: tuple[RegistryEntry, ...] = (
    RegistryEntry("Maarika Kask", "PEP-EE-0077"),
    RegistryEntry("Jonas Lindqvist", "PEP-SE-0214"),
    RegistryEntry("Petra Novak", "PEP-CZ-0033"),
    RegistryEntry("Anders Holm", "PEP-DK-0158"),
)


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


class MockRegistryClient:
    """Fuzzy name-matching client over a static synthetic list.

    ``failure_rate`` injects RegistryUnavailableError with a seeded RNG so
    behaviour stays reproducible in tests (SPEC 12.2).
    """

    def __init__(
        self,
        registry: Literal["sanctions", "pep"],
        entries: tuple[RegistryEntry, ...],
        match_threshold: float = 0.85,
        failure_rate: float = 0.0,
        seed: int = 0,
    ) -> None:
        self.registry: Literal["sanctions", "pep"] = registry
        self._entries = entries
        self._match_threshold = match_threshold
        self._failure_rate = failure_rate
        self._rng = random.Random(seed)

    async def search(self, full_name: str) -> list[RegistryHit]:
        if self._failure_rate > 0 and self._rng.random() < self._failure_rate:
            raise RegistryUnavailableError(f"{self.registry} registry: connection failed")

        query = _normalize(full_name)
        hits: list[RegistryHit] = []
        for entry in self._entries:
            score = SequenceMatcher(None, query, _normalize(entry.full_name)).ratio()
            if score >= self._match_threshold:
                hits.append(
                    RegistryHit(
                        registry=self.registry,
                        matched_name=entry.full_name,
                        score=round(score, 4),
                        list_entry=entry.entry_id,
                    )
                )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits


def build_sanctions_client(failure_rate: float = 0.0, seed: int = 0) -> MockRegistryClient:
    return MockRegistryClient("sanctions", SANCTIONS_ENTRIES, failure_rate=failure_rate, seed=seed)


def build_pep_client(failure_rate: float = 0.0, seed: int = 0) -> MockRegistryClient:
    return MockRegistryClient("pep", PEP_ENTRIES, failure_rate=failure_rate, seed=seed)
