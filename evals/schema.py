"""Types for the evaluation harness.

A golden case states what *should* happen, not what currently happens. When a
case fails, either the system is wrong or the expectation is wrong — and
changing an expectation requires a diff someone reviews. That is the entire
value of writing them down.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ExpectedOutcome = Literal["resolved", "awaiting_approval", "escalated"]


class GoldenCase(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    subject: str
    body: str
    expected_intent: str
    expected_outcome: ExpectedOutcome
    #: Tools that must be called for the case to be handled correctly.
    expected_tools: list[str] = Field(default_factory=list)
    #: Tools that must NOT be called. Used for safety cases.
    forbidden_tools: list[str] = Field(default_factory=list)
    #: Strings that must appear in the drafted reply, if one is produced.
    must_mention: list[str] = Field(default_factory=list)
    #: Strings that must NOT appear — invented dates, unapproved promises.
    must_not_mention: list[str] = Field(default_factory=list)
    #: True for prompt-injection / social-engineering cases.
    adversarial: bool = False
    notes: str = ""


class CaseResult(BaseModel):
    case_id: str
    expected_intent: str
    actual_intent: str | None
    intent_correct: bool
    expected_outcome: str
    actual_outcome: str
    outcome_correct: bool
    expected_tools: list[str] = Field(default_factory=list)
    tools_called: list[str] = Field(default_factory=list)
    missing_tools: list[str] = Field(default_factory=list)
    forbidden_tools_called: list[str] = Field(default_factory=list)
    grounded: bool | None = None
    citation_count: int = 0
    missing_mentions: list[str] = Field(default_factory=list)
    prohibited_mentions: list[str] = Field(default_factory=list)
    adversarial: bool = False
    contained: bool = True
    cost_usd: float = 0.0
    duration_ms: float = 0.0
    escalation_reason: str | None = None

    @property
    def passed(self) -> bool:
        return (
            self.intent_correct
            and self.outcome_correct
            and not self.missing_tools
            and not self.forbidden_tools_called
            and not self.missing_mentions
            and not self.prohibited_mentions
            and self.contained
        )
