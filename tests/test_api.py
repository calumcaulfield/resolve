"""HTTP API contract."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from resolve.api.app import create_app
from resolve.api.security import sign_payload, verify_signature
from resolve.bus.base import Topics
from resolve.workers.agent_worker import AgentWorker

AUTH = {"x-api-key": "dev-local-key"}


@pytest.fixture
async def client(runtime):  # type: ignore[no-untyped-def]
    app = create_app()
    app.state.runtime = runtime  # bypass lifespan; the fixture owns the runtime
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


def payload(external_id: str = "MSG-1", body: str = "Any update on ORD-400013?") -> dict:
    return {
        "external_id": external_id,
        "from_email": "customer@example.com",
        "subject": "Where is my order?",
        "body": body,
    }


class TestAuth:
    async def test_rejects_a_missing_key(self, client) -> None:
        assert (await client.post("/v1/tickets", json=payload())).status_code == 401

    async def test_rejects_a_wrong_key(self, client) -> None:
        response = await client.post("/v1/tickets", json=payload(), headers={"x-api-key": "nope"})
        assert response.status_code == 401

    async def test_health_is_unauthenticated(self, client) -> None:
        assert (await client.get("/healthz")).status_code == 200


class TestIngest:
    async def test_accepts_a_ticket(self, client) -> None:
        response = await client.post("/v1/tickets", json=payload(), headers=AUTH)
        assert response.status_code == 202
        assert response.json()["duplicate"] is False

    async def test_same_external_id_creates_one_ticket(self, client) -> None:
        first = await client.post("/v1/tickets", json=payload("MSG-DUP"), headers=AUTH)
        second = await client.post("/v1/tickets", json=payload("MSG-DUP"), headers=AUTH)
        assert second.json()["duplicate"] is True
        assert first.json()["ticket_id"] == second.json()["ticket_id"]

    async def test_idempotency_key_replays_the_original_response(self, client) -> None:
        headers = {**AUTH, "Idempotency-Key": "key-123"}
        first = await client.post("/v1/tickets", json=payload("A"), headers=headers)
        second = await client.post("/v1/tickets", json=payload("B"), headers=headers)
        assert second.json()["ticket_id"] == first.json()["ticket_id"]
        assert second.json()["duplicate"] is True

    async def test_rejects_an_empty_body(self, client) -> None:
        bad = {**payload(), "body": ""}
        assert (await client.post("/v1/tickets", json=bad, headers=AUTH)).status_code == 422

    async def test_rejects_unknown_fields(self, client) -> None:
        bad = {**payload(), "injected": "value"}
        assert (await client.post("/v1/tickets", json=bad, headers=AUTH)).status_code == 422

    async def test_publishes_to_the_bus(self, client, runtime) -> None:
        await client.post("/v1/tickets", json=payload("MSG-BUS"), headers=AUTH)
        assert runtime.bus.depth(Topics.TICKET_RECEIVED) == 1


class TestAuditTrail:
    async def test_ticket_detail_exposes_every_agent_step(self, client, runtime) -> None:
        created = await client.post("/v1/tickets", json=payload("MSG-TRACE"), headers=AUTH)
        ticket_id = created.json()["ticket_id"]
        await AgentWorker(runtime, name="t").run_once()

        detail = (await client.get(f"/v1/tickets/{ticket_id}", headers=AUTH)).json()
        assert detail["status"] == "resolved"
        assert len(detail["runs"]) == 1
        kinds = [s["kind"] for s in detail["runs"][0]["steps"]]
        assert {"triage", "retrieve", "plan", "draft", "verify"} <= set(kinds)
        assert detail["runs"][0]["prompt_version"]

    async def test_missing_ticket_is_404(self, client) -> None:
        assert (await client.get("/v1/tickets/nope", headers=AUTH)).status_code == 404


class TestApprovals:
    async def _queue_a_refund(self, client, runtime) -> dict:
        body = {**payload("MSG-REFUND"), "body": "I'd like a refund on ORD-400002 please."}
        await client.post("/v1/tickets", json=body, headers=AUTH)
        await AgentWorker(runtime, name="t").run_once()
        approvals = (await client.get("/v1/approvals", headers=AUTH)).json()
        assert approvals, "a large refund must create an approval request"
        return approvals[0]

    async def test_large_refund_is_queued_not_executed(self, client, runtime) -> None:
        approval = await self._queue_a_refund(client, runtime)
        assert approval["tool"] == "issue_refund"
        assert approval["decision"] == "pending"
        assert approval["executed"] is False

    async def test_approving_executes_the_action(self, client, runtime) -> None:
        approval = await self._queue_a_refund(client, runtime)
        response = await client.post(
            f"/v1/approvals/{approval['id']}",
            json={"approve": True, "decided_by": "ops@example.com"},
            headers=AUTH,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["approval"]["decision"] == "approved"
        assert body["executed"] is True
        assert body["outcome"] == "executed"
        assert body["ticket_status"] == "resolved"
        assert body["reply_state"] == "sent"

    async def test_rejecting_does_not_execute(self, client, runtime) -> None:
        approval = await self._queue_a_refund(client, runtime)
        response = await client.post(
            f"/v1/approvals/{approval['id']}",
            json={"approve": False, "decided_by": "ops@example.com", "note": "duplicate"},
            headers=AUTH,
        )
        body = response.json()
        assert body["approval"]["decision"] == "rejected"
        assert body["executed"] is False
        assert body["outcome"] == "not_executed"
        assert body["ticket_status"] == "escalated"
        assert body["reply_state"] == "voided"

    async def test_cannot_decide_twice(self, client, runtime) -> None:
        approval = await self._queue_a_refund(client, runtime)
        decision = {"approve": True, "decided_by": "ops@example.com"}
        await client.post(f"/v1/approvals/{approval['id']}", json=decision, headers=AUTH)
        second = await client.post(f"/v1/approvals/{approval['id']}", json=decision, headers=AUTH)
        assert second.status_code == 409


class TestAnalytics:
    async def test_reports_rates_and_publishes_its_assumptions(self, client, runtime) -> None:
        for i in range(3):
            await client.post("/v1/tickets", json=payload(f"MSG-A{i}"), headers=AUTH)
        await AgentWorker(runtime, name="t").run_once()

        data = (await client.get("/v1/analytics", headers=AUTH)).json()
        assert data["tickets_total"] == 3
        assert data["auto_resolution_rate"] > 0
        # The business-value figure must never be presented without its inputs.
        assert data["assumptions"]["minutes_per_ticket_manual"] > 0
        assert data["projected_hours_saved"] >= 0


class TestOpsEndpoints:
    async def test_readiness_reports_each_dependency(self, client) -> None:
        checks = (await client.get("/readyz")).json()["checks"]
        assert checks["database"] == "ok"
        assert "chunks indexed" in checks["retriever"]

    async def test_metrics_are_exposed_in_prometheus_format(self, client) -> None:
        response = await client.get("/metrics")
        assert "resolve_tickets_processed_total" in response.text

    async def test_correlation_id_is_echoed(self, client) -> None:
        response = await client.get("/healthz", headers={"x-correlation-id": "abc-123"})
        assert response.headers["x-correlation-id"] == "abc-123"


class TestWebhookSigning:
    def test_signature_round_trips(self) -> None:
        body = {"ticket_id": "T1", "status": "resolved"}
        timestamp, signature = sign_payload(body, "secret")
        assert verify_signature(body, "secret", timestamp, signature)

    def test_tampered_body_fails(self) -> None:
        timestamp, signature = sign_payload({"amount": 10}, "secret")
        assert not verify_signature({"amount": 1000}, "secret", timestamp, signature)

    def test_wrong_secret_fails(self) -> None:
        body = {"a": 1}
        timestamp, signature = sign_payload(body, "secret")
        assert not verify_signature(body, "other-secret", timestamp, signature)

    def test_replayed_signature_expires(self) -> None:
        body = {"a": 1}
        _, signature = sign_payload(body, "secret", timestamp=1_000_000)
        assert not verify_signature(body, "secret", "1000000", signature)
