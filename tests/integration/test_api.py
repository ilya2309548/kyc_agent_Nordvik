"""API integration tests over the in-memory backend (SPEC 10)."""

import asyncio
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from kyc_agent.api.main import create_app
from kyc_agent.config import Settings


@pytest.fixture
async def client(golden_cases):  # noqa: ANN201
    settings = Settings(_env_file=None, persistence_backend="memory")
    app = create_app(settings)
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def wait_for_status(
    http: AsyncClient, case_id: str, target: str, timeout: float = 5.0
) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        response = await http.get(f"/api/v1/cases/{case_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] == target:
            return body
        assert asyncio.get_event_loop().time() < deadline, (
            f"case {case_id} stuck in {body['status']!r}, wanted {target!r}"
        )
        await asyncio.sleep(0.05)


class TestCaseLifecycle:
    async def test_clean_case_completes_automatically(self, client, golden_cases) -> None:
        case = golden_cases["clean-individual-1"]
        response = await client.post("/api/v1/cases", json=case["package"])
        assert response.status_code == 202
        case_id = response.json()["case_id"]

        body = await wait_for_status(client, case_id, "completed")
        assert body["decision"]["outcome"] == "approve"
        assert body["decision"]["decided_by"] == "system"
        assert body["review_request"] is None

    async def test_escalated_case_review_roundtrip(self, client, golden_cases) -> None:
        case = golden_cases["sanctions-hit"]
        response = await client.post("/api/v1/cases", json=case["package"])
        case_id = response.json()["case_id"]

        body = await wait_for_status(client, case_id, "awaiting_human_review")
        assert "SANCTIONS_HIT" in body["review_request"]["reason_codes"]
        assert body["risk_level"] == "high"

        review = await client.post(
            f"/api/v1/cases/{case_id}/review",
            json={"outcome": "reject", "reviewer": "j.doe", "comment": "EU list match"},
        )
        assert review.status_code == 200
        reviewed = review.json()
        assert reviewed["status"] == "completed"
        assert reviewed["decision"]["decided_by"] == "human"
        assert reviewed["decision"]["reviewer"] == "j.doe"

    async def test_audit_trail_is_served(self, client, golden_cases) -> None:
        case = golden_cases["clean-individual-1"]
        response = await client.post("/api/v1/cases", json=case["package"])
        case_id = response.json()["case_id"]
        await wait_for_status(client, case_id, "completed")

        audit = await client.get(f"/api/v1/cases/{case_id}/audit")
        assert audit.status_code == 200
        events = audit.json()
        event_types = [e["event_type"] for e in events]
        assert "decision_made" in event_types
        assert "case_completed" in event_types
        assert any(e["event_type"] == "registry_checked" for e in events)


class TestApiErrors:
    async def test_unknown_case_is_404(self, client) -> None:
        assert (await client.get("/api/v1/cases/nope")).status_code == 404
        response = await client.post(
            "/api/v1/cases/nope/review",
            json={"outcome": "approve", "reviewer": "x"},
        )
        assert response.status_code == 404

    async def test_review_of_completed_case_is_409(self, client, golden_cases) -> None:
        case = golden_cases["clean-individual-1"]
        response = await client.post("/api/v1/cases", json=case["package"])
        case_id = response.json()["case_id"]
        await wait_for_status(client, case_id, "completed")

        review = await client.post(
            f"/api/v1/cases/{case_id}/review",
            json={"outcome": "approve", "reviewer": "j.doe"},
        )
        assert review.status_code == 409

    async def test_invalid_review_outcome_is_422(self, client, golden_cases) -> None:
        case = golden_cases["pep-match"]
        response = await client.post("/api/v1/cases", json=case["package"])
        case_id = response.json()["case_id"]
        await wait_for_status(client, case_id, "awaiting_human_review")

        review = await client.post(
            f"/api/v1/cases/{case_id}/review",
            json={"outcome": "looks-fine", "reviewer": "j.doe"},
        )
        assert review.status_code == 422

    async def test_invalid_package_is_422(self, client) -> None:
        response = await client.post("/api/v1/cases", json={"customer_type": "alien"})
        assert response.status_code == 422

    async def test_health(self, client) -> None:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "memory"}
