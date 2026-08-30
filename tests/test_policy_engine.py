"""The policy engine decides whether money can move. It gets its own file."""

from __future__ import annotations

import pytest

from resolve.agent.policy import PolicyEngine, PolicyOutcome
from resolve.agent.tools.commerce_tools import build_commerce_registry
from resolve.services.commerce import InMemoryCommerceBackend


@pytest.fixture
def policy() -> PolicyEngine:
    return PolicyEngine(
        registry=build_commerce_registry(InMemoryCommerceBackend()),
        refund_auto_approve_limit_gbp=25.0,
        min_confidence_for_auto_action=0.7,
    )


def test_read_only_tools_run_without_approval(policy: PolicyEngine) -> None:
    assert policy.evaluate("lookup_order", {"order_ref": "ORD-1"}).allowed


def test_small_refund_is_auto_approved(policy: PolicyEngine) -> None:
    verdict = policy.evaluate("issue_refund", {"amount_gbp": 12.50})
    assert verdict.outcome is PolicyOutcome.ALLOW


def test_large_refund_requires_a_human(policy: PolicyEngine) -> None:
    verdict = policy.evaluate("issue_refund", {"amount_gbp": 480.00})
    assert verdict.outcome is PolicyOutcome.REQUIRE_APPROVAL
    assert "exceeds" in verdict.reason


def test_confidence_cannot_unlock_a_large_refund(policy: PolicyEngine) -> None:
    """The whole design premise: capability gates, not confidence gates."""
    verdict = policy.evaluate("issue_refund", {"amount_gbp": 480.00}, confidence=0.999)
    assert verdict.outcome is PolicyOutcome.REQUIRE_APPROVAL


def test_low_confidence_blocks_an_otherwise_allowed_refund(policy: PolicyEngine) -> None:
    verdict = policy.evaluate("issue_refund", {"amount_gbp": 5.00}, confidence=0.4)
    assert verdict.outcome is PolicyOutcome.REQUIRE_APPROVAL


def test_injection_locks_write_tools(policy: PolicyEngine) -> None:
    verdict = policy.evaluate(
        "issue_refund", {"amount_gbp": 5.00}, confidence=0.99, injection_detected=True
    )
    assert verdict.outcome is PolicyOutcome.REQUIRE_APPROVAL


def test_injection_leaves_read_tools_available(policy: PolicyEngine) -> None:
    """A human triaging the escalation still benefits from the order context."""
    verdict = policy.evaluate("lookup_order", {"order_ref": "ORD-1"}, injection_detected=True)
    assert verdict.allowed


def test_unknown_tool_is_denied(policy: PolicyEngine) -> None:
    assert policy.evaluate("drop_database", {}).outcome is PolicyOutcome.DENY


def test_negative_refund_is_denied(policy: PolicyEngine) -> None:
    assert policy.evaluate("issue_refund", {"amount_gbp": -50}).outcome is PolicyOutcome.DENY
