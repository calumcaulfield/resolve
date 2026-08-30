"""The human-decision lifecycle.

A rejection is not a status change. It is a domain event with consequences for
the approval, the run, the drafted reply, the ticket and the audit trail, and
the bug these tests pin was that only the first of those was being recorded:

* the trace stopped at "queued for human approval" with no human event;
* `escalation_reason` still claimed the ticket was awaiting approval;
* the drafted reply — written on the assumption the refund *would* happen —
  was still presented as the outcome.

Each of those is asserted below, plus the invariant that matters most: a
rejected action must never appear to have executed.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from resolve.api.app import create_app
from resolve.db.models import Approval
from resolve.db.session import session_scope
from resolve.domain.enums import ActorType, ApprovalOutcome, ReplyState, TicketStatus
from resolve.workers.agent_worker import AgentWorker

AUTH = {"x-api-key": "dev-local-key"}


def message(external_id: str, body: str, subject: str = "Refund request") -> dict:
    return {
        "external_id": external_id,
        "from_email": "customer@example.com",
        "subject": subject,
        "body": body,
    }


@pytest.fixture
async def client(runtime):  # type: ignore[no-untyped-def]
    app = create_app()
    app.state.runtime = runtime
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def queue_refund(client, runtime, external_id: str = "MSG-R1") -> dict:
    """Produce a ticket parked on a refund approval."""
    await client.post(
        "/v1/tickets",
        json=message(external_id, "I'd like a refund on ORD-400002 please."),
        headers=AUTH,
    )
    await AgentWorker(runtime, name="t").run_once()
    approvals = (await client.get("/v1/approvals", headers=AUTH)).json()
    assert approvals, "a large refund must queue an approval"
    return approvals[0]


class TestApprovalRequestAudit:
    """The request itself must carry enough metadata to be auditable."""

    async def test_records_action_resource_amount_and_risk(self, client, runtime) -> None:
        approval = await queue_refund(client, runtime)
        assert approval["tool"] == "issue_refund"
        assert approval["resource"] == "ORD-400002"
        assert approval["amount_gbp"] == pytest.approx(207.0)
        assert approval["risk"] == "requires_approval"
        assert approval["decision"] == "pending"
        assert approval["outcome"] == "pending"

    async def test_reply_starts_awaiting_approval_not_sent(self, client, runtime) -> None:
        approval = await queue_refund(client, runtime)
        detail = (await client.get(f"/v1/tickets/{approval['ticket_id']}", headers=AUTH)).json()
        run = detail["runs"][0]
        assert run["reply_body"], "a draft exists"
        assert run["reply_state"] == ReplyState.AWAITING_APPROVAL.value
        assert run["reply_state"] != ReplyState.SENT.value

    async def test_context_endpoint_gives_a_reviewer_what_they_need(self, client, runtime) -> None:
        approval = await queue_refund(client, runtime)
        ctx = (await client.get(f"/v1/approvals/{approval['id']}", headers=AUTH)).json()
        assert ctx["customer_message"], "the customer's actual words"
        assert ctx["ai_rationale"], "why the agent proposed it"
        assert ctx["policy_citations"], "the policy it retrieved"
        assert ctx["proposed_reply_body"], "what will be sent if approved"
        assert ctx["approval"]["amount_gbp"] == pytest.approx(207.0)


class TestRejection:
    """The bug, pinned from every angle."""

    async def test_action_is_not_executed(self, client, runtime) -> None:
        approval = await queue_refund(client, runtime)
        body = (
            await client.post(
                f"/v1/approvals/{approval['id']}",
                json={"approve": False, "decided_by": "ops@example.com", "note": "paid by phone"},
                headers=AUTH,
            )
        ).json()
        assert body["executed"] is False
        assert body["outcome"] == ApprovalOutcome.NOT_EXECUTED.value
        assert body["approval"]["execution_result"] is None

    async def test_no_money_moved(self, client, runtime) -> None:
        """The strongest form of the invariant: check the commerce backend."""
        approval = await queue_refund(client, runtime)
        order_before = await runtime.application.commerce.get_order("ORD-400002")
        refunded_before = order_before.refunded_gbp

        await client.post(
            f"/v1/approvals/{approval['id']}",
            json={"approve": False, "decided_by": "ops@example.com"},
            headers=AUTH,
        )
        order_after = await runtime.application.commerce.get_order("ORD-400002")
        assert order_after.refunded_gbp == refunded_before

    async def test_trace_records_the_human_decision(self, client, runtime) -> None:
        approval = await queue_refund(client, runtime)
        await client.post(
            f"/v1/approvals/{approval['id']}",
            json={"approve": False, "decided_by": "ops@example.com", "note": "paid by phone"},
            headers=AUTH,
        )
        detail = (await client.get(f"/v1/tickets/{approval['ticket_id']}", headers=AUTH)).json()
        kinds = [s["kind"] for s in detail["runs"][0]["steps"]]

        assert "human_decision" in kinds, "the trace must contain the human decision"
        assert "not_executed" in kinds, "and state explicitly that nothing ran"
        assert "escalate" in kinds, "and why the ticket ended up escalated"

    async def test_the_human_decision_step_is_attributed_and_detailed(
        self, client, runtime
    ) -> None:
        approval = await queue_refund(client, runtime)
        await client.post(
            f"/v1/approvals/{approval['id']}",
            json={
                "approve": False,
                "decided_by": "alex@example.com",
                "note": "Customer already refunded by phone",
            },
            headers=AUTH,
        )
        detail = (await client.get(f"/v1/tickets/{approval['ticket_id']}", headers=AUTH)).json()
        step = next(s for s in detail["runs"][0]["steps"] if s["kind"] == "human_decision")

        assert step["actor_type"] == ActorType.HUMAN.value
        assert step["actor"] == "alex@example.com"
        assert step["detail"]["decision"] == "rejected"
        assert step["detail"]["action"] == "issue_refund"
        assert step["detail"]["resource"] == "ORD-400002"
        assert step["detail"]["reason"] == "Customer already refunded by phone"
        assert step["detail"]["decided_at"]

    async def test_provisional_draft_is_voided_not_sent(self, client, runtime) -> None:
        """The reply said the refund had been processed. It had not."""
        approval = await queue_refund(client, runtime)
        await client.post(
            f"/v1/approvals/{approval['id']}",
            json={"approve": False, "decided_by": "ops@example.com"},
            headers=AUTH,
        )
        run = (await client.get(f"/v1/tickets/{approval['ticket_id']}", headers=AUTH)).json()[
            "runs"
        ][0]

        assert run["reply_state"] == ReplyState.VOIDED.value
        assert run["reply_state"] != ReplyState.SENT.value
        assert run["reply_state_reason"], "the void must explain itself"
        # Retained for audit, but never as an outcome.
        assert run["reply_body"]

    async def test_escalation_reason_explains_the_rejection(self, client, runtime) -> None:
        approval = await queue_refund(client, runtime)
        await client.post(
            f"/v1/approvals/{approval['id']}",
            json={"approve": False, "decided_by": "ops@example.com"},
            headers=AUTH,
        )
        run = (await client.get(f"/v1/tickets/{approval['ticket_id']}", headers=AUTH)).json()[
            "runs"
        ][0]

        reason = run["escalation_reason"] or ""
        assert "rejected" in reason.lower()
        # The stale message the bug left behind.
        assert "require human approval before the reply can be sent" not in reason

    async def test_ticket_status_is_escalated(self, client, runtime) -> None:
        approval = await queue_refund(client, runtime)
        await client.post(
            f"/v1/approvals/{approval['id']}",
            json={"approve": False, "decided_by": "ops@example.com"},
            headers=AUTH,
        )
        detail = (await client.get(f"/v1/tickets/{approval['ticket_id']}", headers=AUTH)).json()
        assert detail["status"] == TicketStatus.ESCALATED.value
        assert detail["runs"][0]["status"] == TicketStatus.ESCALATED.value


class TestApproval:
    async def test_action_executes_and_reply_is_sent(self, client, runtime) -> None:
        approval = await queue_refund(client, runtime)
        body = (
            await client.post(
                f"/v1/approvals/{approval['id']}",
                json={"approve": True, "decided_by": "ops@example.com"},
                headers=AUTH,
            )
        ).json()
        assert body["executed"] is True
        assert body["outcome"] == ApprovalOutcome.EXECUTED.value
        assert body["reply_state"] == ReplyState.SENT.value
        assert body["ticket_status"] == TicketStatus.RESOLVED.value

    async def test_money_actually_moved(self, client, runtime) -> None:
        approval = await queue_refund(client, runtime)
        before = (await runtime.application.commerce.get_order("ORD-400002")).refunded_gbp
        await client.post(
            f"/v1/approvals/{approval['id']}",
            json={"approve": True, "decided_by": "ops@example.com"},
            headers=AUTH,
        )
        after = (await runtime.application.commerce.get_order("ORD-400002")).refunded_gbp
        assert after > before

    async def test_trace_records_the_execution(self, client, runtime) -> None:
        approval = await queue_refund(client, runtime)
        await client.post(
            f"/v1/approvals/{approval['id']}",
            json={"approve": True, "decided_by": "ops@example.com"},
            headers=AUTH,
        )
        steps = (await client.get(f"/v1/tickets/{approval['ticket_id']}", headers=AUTH)).json()[
            "runs"
        ][0]["steps"]
        kinds = [s["kind"] for s in steps]
        assert "human_decision" in kinds
        assert "execute" in kinds

        execute = next(s for s in steps if s["kind"] == "execute")
        assert execute["actor_type"] == ActorType.SYSTEM.value
        assert execute["detail"]["authorised_by"] == "ops@example.com"


class TestIdempotency:
    """A double-click must not issue two refunds."""

    async def test_second_decision_is_409(self, client, runtime) -> None:
        approval = await queue_refund(client, runtime)
        payload = {"approve": True, "decided_by": "ops@example.com"}
        first = await client.post(f"/v1/approvals/{approval['id']}", json=payload, headers=AUTH)
        second = await client.post(f"/v1/approvals/{approval['id']}", json=payload, headers=AUTH)
        assert first.status_code == 200
        assert second.status_code == 409

    async def test_the_action_runs_exactly_once(self, client, runtime) -> None:
        approval = await queue_refund(client, runtime)
        payload = {"approve": True, "decided_by": "ops@example.com"}
        await client.post(f"/v1/approvals/{approval['id']}", json=payload, headers=AUTH)
        after_first = (await runtime.application.commerce.get_order("ORD-400002")).refunded_gbp
        await client.post(f"/v1/approvals/{approval['id']}", json=payload, headers=AUTH)
        after_second = (await runtime.application.commerce.get_order("ORD-400002")).refunded_gbp
        assert after_first == after_second

    async def test_reversing_a_decision_is_refused(self, client, runtime) -> None:
        approval = await queue_refund(client, runtime)
        await client.post(
            f"/v1/approvals/{approval['id']}",
            json={"approve": False, "decided_by": "ops@example.com"},
            headers=AUTH,
        )
        retry = await client.post(
            f"/v1/approvals/{approval['id']}",
            json={"approve": True, "decided_by": "someone.else@example.com"},
            headers=AUTH,
        )
        assert retry.status_code == 409


class TestMixedDecisions:
    """Deciding each approval in isolation let the last decision win."""

    async def test_one_rejection_escalates_the_whole_ticket(self, client, runtime) -> None:
        approval = await queue_refund(client, runtime)

        # Attach a second pending approval to the same ticket.
        async with session_scope(runtime.session_factory) as session:
            original = await session.get(Approval, approval["id"])
            assert original is not None
            session.add(
                Approval(
                    id="second-approval",
                    ticket_id=original.ticket_id,
                    run_id=original.run_id,
                    tool="cancel_order",
                    arguments={"order_ref": "ORD-400002"},
                    rationale="Cancel the order too",
                    resource="ORD-400002",
                )
            )

        # Approve one, reject the other.
        await client.post(
            "/v1/approvals/second-approval",
            json={"approve": True, "decided_by": "ops@example.com"},
            headers=AUTH,
        )
        final = (
            await client.post(
                f"/v1/approvals/{approval['id']}",
                json={"approve": False, "decided_by": "ops@example.com"},
                headers=AUTH,
            )
        ).json()

        assert final["ticket_status"] == TicketStatus.ESCALATED.value
        assert final["reply_state"] == ReplyState.VOIDED.value

    async def test_ticket_waits_while_any_approval_is_pending(self, client, runtime) -> None:
        approval = await queue_refund(client, runtime)
        async with session_scope(runtime.session_factory) as session:
            original = await session.get(Approval, approval["id"])
            assert original is not None
            session.add(
                Approval(
                    id="still-pending",
                    ticket_id=original.ticket_id,
                    run_id=original.run_id,
                    tool="cancel_order",
                    arguments={"order_ref": "ORD-400002"},
                    rationale="Cancel the order too",
                )
            )

        body = (
            await client.post(
                f"/v1/approvals/{approval['id']}",
                json={"approve": True, "decided_by": "ops@example.com"},
                headers=AUTH,
            )
        ).json()
        assert body["ticket_status"] == TicketStatus.AWAITING_APPROVAL.value
        assert body["reply_state"] == ReplyState.AWAITING_APPROVAL.value


class TestExecutionFailure:
    """Approved, attempted, refused downstream is a third outcome."""

    async def test_failed_execution_holds_the_reply_rather_than_sending_it(
        self, client, runtime
    ) -> None:
        approval = await queue_refund(client, runtime)

        # Refund the order fully first, so the approved refund is now refused
        # by the commerce backend's own rules.
        await runtime.application.commerce.issue_refund("ORD-400002", 207.0, "manual")

        body = (
            await client.post(
                f"/v1/approvals/{approval['id']}",
                json={"approve": True, "decided_by": "ops@example.com"},
                headers=AUTH,
            )
        ).json()

        assert body["executed"] is False
        assert body["outcome"] == ApprovalOutcome.EXECUTION_FAILED.value
        assert body["ticket_status"] == TicketStatus.ESCALATED.value
        # Held, not voided: the draft may be partly salvageable by a human.
        assert body["reply_state"] == ReplyState.HELD.value


class TestOversightMetrics:
    """Counted from real decisions, and kept apart from evaluation."""

    async def test_empty_before_any_decision(self, client, runtime) -> None:
        await queue_refund(client, runtime)
        m = (await client.get("/v1/oversight", headers=AUTH)).json()
        assert m["pending"] >= 1
        assert m["approved"] == 0
        assert m["rejected"] == 0
        assert m["approval_rate"] == 0.0
        # Never 0.0, which would read as "decided instantly".
        assert m["median_time_to_decision_seconds"] is None

    async def test_reflects_decisions(self, client, runtime) -> None:
        approval = await queue_refund(client, runtime)
        await client.post(
            f"/v1/approvals/{approval['id']}",
            json={"approve": False, "decided_by": "ops@example.com"},
            headers=AUTH,
        )
        m = (await client.get("/v1/oversight", headers=AUTH)).json()
        assert m["rejected"] == 1
        assert m["rejection_rate"] == 1.0
        assert m["override_rate"] == 1.0
        assert m["total_value_rejected_gbp"] == pytest.approx(207.0)

        refunds = next(a for a in m["by_action"] if a["action"] == "issue_refund")
        assert refunds["proposed"] >= 1
        assert refunds["rejected"] == 1

    async def test_requires_authentication(self, client) -> None:
        assert (await client.get("/v1/oversight")).status_code == 401


class TestAuditLog:
    async def test_records_the_decision(self, client, runtime) -> None:
        approval = await queue_refund(client, runtime)
        await client.post(
            f"/v1/approvals/{approval['id']}",
            json={"approve": False, "decided_by": "alex@example.com", "note": "duplicate"},
            headers=AUTH,
        )
        events = (await client.get("/v1/audit", headers=AUTH)).json()
        assert len(events) == 1

        event = events[0]
        assert event["action"] == "issue_refund"
        assert event["resource"] == "ORD-400002"
        assert event["decision"] == "rejected"
        assert event["outcome"] == "not_executed"
        assert event["actor"] == "alex@example.com"
        assert event["actor_type"] == "human"
        assert event["note"] == "duplicate"
        assert event["decided_at"]

    async def test_pending_approvals_are_not_audit_events(self, client, runtime) -> None:
        await queue_refund(client, runtime)
        assert (await client.get("/v1/audit", headers=AUTH)).json() == []

    async def test_requires_authentication(self, client) -> None:
        assert (await client.get("/v1/audit")).status_code == 401


class TestEvaluationIsUnaffected:
    """A human rejecting an operational action must not touch evaluation."""

    async def test_rejection_does_not_change_evaluation_state(self, client, runtime) -> None:
        approval = await queue_refund(client, runtime)
        before = (await client.get("/v1/evals/latest", headers=AUTH)).status_code
        await client.post(
            f"/v1/approvals/{approval['id']}",
            json={"approve": False, "decided_by": "ops@example.com"},
            headers=AUTH,
        )
        after = (await client.get("/v1/evals/latest", headers=AUTH)).status_code
        assert before == after, "operational decisions must not affect the eval report"
