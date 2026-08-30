"""The gate between what the agent wants to do and what it may do.

Design principle: **capability, not confidence.** The model's stated
confidence never unlocks a capability. A £480 refund requires a human whether
the model is 0.51 or 0.99 confident, because the cost of being wrong does not
scale with the model's self-report — and a prompt-injection attack produces a
very confident model.

Everything here is deterministic, testable, and reviewable by someone who does
not read Python well. That is the point: the rules a business cares about
should not be buried in a prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from resolve.agent.tools.registry import ToolRegistry
from resolve.domain.enums import ActionRisk


class PolicyOutcome(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyVerdict:
    outcome: PolicyOutcome
    reason: str
    risk: ActionRisk = ActionRisk.AUTO

    @property
    def allowed(self) -> bool:
        return self.outcome is PolicyOutcome.ALLOW


class ApprovalRequiredError(Exception):
    def __init__(self, tool: str, reason: str) -> None:
        super().__init__(reason)
        self.tool = tool
        self.reason = reason


@dataclass
class PolicyEngine:
    registry: ToolRegistry
    refund_auto_approve_limit_gbp: float = 25.0
    min_confidence_for_auto_action: float = 0.70
    #: When an injection attempt is detected, nothing consequential runs.
    injection_locks_write_tools: bool = True

    def evaluate(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        confidence: float = 1.0,
        injection_detected: bool = False,
    ) -> PolicyVerdict:
        tool = self.registry.get(tool_name)
        if tool is None:
            return PolicyVerdict(
                PolicyOutcome.DENY, f"unknown tool {tool_name!r}", ActionRisk.FORBIDDEN
            )
        if tool.risk is ActionRisk.FORBIDDEN:
            return PolicyVerdict(
                PolicyOutcome.DENY, f"{tool_name} is disabled by policy", ActionRisk.FORBIDDEN
            )

        is_write = tool.risk is ActionRisk.REQUIRES_APPROVAL

        # 1. Injection: read-only tools stay available (they help a human
        #    understand the ticket); nothing that changes state runs.
        if injection_detected and is_write and self.injection_locks_write_tools:
            return PolicyVerdict(
                PolicyOutcome.REQUIRE_APPROVAL,
                "possible prompt-injection detected in the customer message; "
                "state-changing actions require human review",
                tool.risk,
            )

        # 2. Low confidence never unlocks a write. It can still block one.
        if is_write and confidence < self.min_confidence_for_auto_action:
            return PolicyVerdict(
                PolicyOutcome.REQUIRE_APPROVAL,
                f"classification confidence {confidence:.2f} is below the "
                f"{self.min_confidence_for_auto_action:.2f} threshold for automated action",
                tool.risk,
            )

        # 3. Money: the only capability with a value-scaled auto-approval.
        if tool_name == "issue_refund":
            amount = float(arguments.get("amount_gbp") or 0.0)
            if amount <= 0:
                return PolicyVerdict(
                    PolicyOutcome.DENY, "refund amount must be positive", tool.risk
                )
            if amount > self.refund_auto_approve_limit_gbp:
                return PolicyVerdict(
                    PolicyOutcome.REQUIRE_APPROVAL,
                    f"refund of £{amount:.2f} exceeds the £"
                    f"{self.refund_auto_approve_limit_gbp:.2f} automatic limit",
                    tool.risk,
                )
            return PolicyVerdict(
                PolicyOutcome.ALLOW,
                f"refund of £{amount:.2f} is within the automatic limit",
                tool.risk,
            )

        if is_write:
            return PolicyVerdict(
                PolicyOutcome.REQUIRE_APPROVAL,
                f"{tool_name} changes fulfilment and requires human approval",
                tool.risk,
            )

        return PolicyVerdict(PolicyOutcome.ALLOW, "read-only action", tool.risk)
