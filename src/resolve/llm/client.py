"""The LLM client every caller in the system actually uses.

Adds, on top of the raw provider:

* **Model routing** — cheap model for classification, strong model for
  reasoning and drafting. Roughly 70% of calls in a typical ticket are
  classification-shaped, so this is where most of the cost saving is (ADR-006).
* **Schema-repair retry** — on a validation failure the schema error is fed
  back to the model once before giving up. The original Proflow implementation
  discovered the need for this the hard way and handled it with a regex that
  stripped markdown fences; doing it properly is three lines and a test.
* **Response cache** — identical (task, model, prompt) inside the TTL is free
  and instant. Support inboxes repeat themselves constantly.
* **Cost ledger** — every call is recorded so a per-ticket spend figure and a
  budget ceiling are both possible. Nothing else in this portfolio measures
  what an LLM feature costs; this does.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from enum import StrEnum
from typing import TypeVar

from pydantic import BaseModel

from resolve.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    Role,
    StructuredOutputError,
    Usage,
)
from resolve.logging import get_logger

TModel = TypeVar("TModel", bound=BaseModel)

log = get_logger(__name__)


class ModelTier(StrEnum):
    """Which model class a call needs.

    The mapping tier → concrete model id lives in configuration, so switching
    vendors or upgrading a model is a config change, not a code change.
    """

    FAST = "fast"
    """Classification, extraction, routing. Cheap model."""

    REASONING = "reasoning"
    """Planning, drafting, verification. Strong model."""


class _CacheEntry(BaseModel):
    value: str
    expires_at: float


class ResponseCache:
    """Small in-process TTL cache keyed by a hash of the request.

    Deliberately not Redis: the cache is a latency and cost optimisation, not
    a correctness mechanism, and a per-process cache keeps the dependency
    surface of the agent worker small. Swapping in a shared cache is a
    single-class change.
    """

    def __init__(self, ttl_seconds: int = 3600, max_entries: int = 2048) -> None:
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._data: dict[str, _CacheEntry] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(request: LLMRequest, schema_name: str = "") -> str:
        material = f"{schema_name}\x1f{request.cache_key_material()}"
        return hashlib.sha256(material.encode()).hexdigest()

    def get(self, key: str) -> str | None:
        entry = self._data.get(key)
        if entry is None:
            self.misses += 1
            return None
        if entry.expires_at < time.time():
            del self._data[key]
            self.misses += 1
            return None
        self.hits += 1
        return entry.value

    def set(self, key: str, value: str) -> None:
        if len(self._data) >= self.max_entries:
            # Evict the soonest-to-expire entry. O(n) but n is small and this
            # runs far less often than `get`.
            oldest = min(self._data, key=lambda k: self._data[k].expires_at)
            del self._data[oldest]
        self._data[key] = _CacheEntry(value=value, expires_at=time.time() + self.ttl)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total else 0.0


class CostLedger:
    """Append-only record of model spend, per ticket and in aggregate."""

    def __init__(self) -> None:
        self.entries: list[Usage] = []

    def record(self, usage: Usage) -> None:
        self.entries.append(usage)

    @property
    def total_usd(self) -> float:
        return round(sum(e.cost_usd for e in self.entries), 8)

    @property
    def total_input_tokens(self) -> int:
        return sum(e.input_tokens for e in self.entries)

    @property
    def total_output_tokens(self) -> int:
        return sum(e.output_tokens for e in self.entries)

    def by_model(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for e in self.entries:
            out[e.model] = round(out.get(e.model, 0.0) + e.cost_usd, 8)
        return out

    def reset(self) -> None:
        self.entries.clear()


class LLMClient:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        fast_model: str,
        reasoning_model: str,
        max_retries: int = 2,
        cache: ResponseCache | None = None,
        max_output_tokens: int = 2048,
    ) -> None:
        self.provider = provider
        self.models: dict[ModelTier, str] = {
            ModelTier.FAST: fast_model,
            ModelTier.REASONING: reasoning_model,
        }
        self.max_retries = max_retries
        self.cache = cache
        self.max_output_tokens = max_output_tokens
        self.ledger = CostLedger()

    # -- public API -------------------------------------------------------

    async def complete(
        self,
        *,
        task: str,
        system: str,
        user: str,
        tier: ModelTier = ModelTier.REASONING,
    ) -> LLMResponse:
        request = LLMRequest(
            task=task,
            system=system,
            messages=[Message(role=Role.USER, content=user)],
            model=self.models[tier],
            max_output_tokens=self.max_output_tokens,
        )
        response = await self.provider.complete(request)
        self.ledger.record(response.usage)
        return response

    async def structured(
        self,
        schema: type[TModel],
        *,
        task: str,
        system: str,
        user: str,
        tier: ModelTier = ModelTier.REASONING,
        metadata: dict[str, object] | None = None,
    ) -> tuple[TModel, Usage]:
        """Get a validated instance of `schema`, retrying schema failures once.

        Raises `StructuredOutputError` if the model cannot produce a valid
        object within `max_retries`. Callers treat that as an escalation
        trigger, never as a reason to guess.
        """
        request = LLMRequest(
            task=task,
            system=system,
            messages=[Message(role=Role.USER, content=user)],
            model=self.models[tier],
            max_output_tokens=self.max_output_tokens,
            metadata=dict(metadata or {}),
        )

        cache_key = ResponseCache.key(request, schema.__name__) if self.cache else None
        if cache_key and self.cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                usage = Usage(model=request.model or "", cache_hit=True)
                self.ledger.record(usage)
                log.debug("llm.cache_hit", task=task, model=request.model)
                return schema.model_validate_json(cached), usage

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                value, usage = await self.provider.complete_structured(request, schema)
            except StructuredOutputError as exc:
                last_error = exc
                log.warning(
                    "llm.schema_violation",
                    task=task,
                    attempt=attempt + 1,
                    error=str(exc),
                )
                if attempt >= self.max_retries:
                    break
                # Repair turn: hand the model its own error. Cheaper and far
                # more reliable than post-hoc string surgery on the output.
                request = request.model_copy(
                    update={
                        "messages": [
                            *request.messages,
                            Message(
                                role=Role.USER,
                                content=(
                                    "Your previous response did not satisfy the required "
                                    f"schema. Error: {exc}. Respond again with valid data "
                                    "matching the schema exactly. Do not explain."
                                ),
                            ),
                        ]
                    }
                )
                await asyncio.sleep(0.1 * (2**attempt))
                continue

            self.ledger.record(usage)
            if cache_key and self.cache:
                self.cache.set(cache_key, value.model_dump_json())
            return value, usage

        raise StructuredOutputError(
            f"task {task!r} failed schema validation after "
            f"{self.max_retries + 1} attempts: {last_error}"
        )

    # -- accounting -------------------------------------------------------

    def spend_usd(self) -> float:
        return self.ledger.total_usd

    def reset_ledger(self) -> None:
        self.ledger.reset()


def build_provider(
    kind: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = 60.0,
    max_retries: int = 3,
) -> LLMProvider:
    """Factory. Keeps vendor imports lazy so optional extras stay optional."""
    if kind == "mock":
        from resolve.llm.mock_provider import MockProvider

        return MockProvider()
    if kind == "anthropic":
        from resolve.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries
        )
    if kind == "openai":
        from resolve.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries
        )
    raise ValueError(f"unknown LLM provider: {kind!r}")
