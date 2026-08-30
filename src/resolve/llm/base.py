"""Provider-agnostic LLM interface.

Nothing above this module imports a vendor SDK. That is what makes the system
testable: `MockProvider` satisfies the same protocol as `AnthropicProvider`,
so the agent loop, the verifier and the whole eval harness run offline, for
free, deterministically. See ADR-002.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, Field

TModel = TypeVar("TModel", bound=BaseModel)


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class Message(BaseModel):
    role: Role
    content: str


class Usage(BaseModel):
    """Token accounting for one provider call."""

    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float = 0.0
    cache_hit: bool = False

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            model=self.model or other.model,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cost_usd=round(self.cost_usd + other.cost_usd, 8),
            cache_hit=self.cache_hit and other.cache_hit,
        )


class LLMRequest(BaseModel):
    """One unit of work sent to a provider.

    `task` is a stable identifier for the prompt's purpose ("triage",
    "plan", "draft_reply", "verify"). It keys the prompt registry, the
    response cache, the cost ledger, and the mock provider's dispatch table.
    """

    task: str
    system: str
    messages: list[Message] = Field(default_factory=list)
    max_output_tokens: int = 2048
    model: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def cache_key_material(self) -> str:
        parts = [self.task, self.model or "", self.system]
        parts.extend(f"{m.role}:{m.content}" for m in self.messages)
        return "\x1f".join(parts)


class LLMResponse(BaseModel):
    text: str = ""
    usage: Usage = Field(default_factory=Usage)
    raw: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    """The only thing the rest of the system knows about a model vendor."""

    name: str

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Free-form text completion."""
        ...

    async def complete_structured(
        self, request: LLMRequest, schema: type[TModel]
    ) -> tuple[TModel, Usage]:
        """Completion constrained to a Pydantic schema.

        Implementations must raise `StructuredOutputError` rather than return
        a partially-valid object. The caller decides whether to repair-retry.
        """
        ...


class LLMError(Exception):
    """Base class for provider failures."""


class StructuredOutputError(LLMError):
    """The provider returned something that does not satisfy the schema."""

    def __init__(self, message: str, payload: str = "") -> None:
        super().__init__(message)
        self.payload = payload


class BudgetExceededError(LLMError):
    """The per-ticket spend ceiling was reached. The agent escalates."""
