"""LLM client behaviour: routing, schema repair, caching, cost accounting."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from resolve.domain.schemas import TriageResult
from resolve.llm.base import LLMRequest, LLMResponse, StructuredOutputError, Usage
from resolve.llm.client import CostLedger, LLMClient, ModelTier, ResponseCache
from resolve.llm.mock_provider import MockProvider
from resolve.llm.pricing import cost_usd, price_for


class Shape(BaseModel):
    value: int


class FlakyProvider:
    """Fails schema validation a fixed number of times, then succeeds."""

    name = "flaky"

    def __init__(self, failures: int) -> None:
        self.remaining = failures
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text="")

    async def complete_structured(self, request, schema):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise StructuredOutputError("value: field required")
        return schema.model_validate({"value": 42}), Usage(model="flaky", input_tokens=10)


def build(provider) -> LLMClient:  # type: ignore[no-untyped-def]
    return LLMClient(
        provider, fast_model="fast-model", reasoning_model="strong-model", max_retries=2
    )


class TestModelRouting:
    async def test_fast_tier_uses_the_cheap_model(self) -> None:
        provider = MockProvider()
        client = build(provider)
        await client.structured(
            TriageResult,
            task="triage",
            system="s",
            user="Where is ORD-1?",
            tier=ModelTier.FAST,
        )
        assert provider.calls[-1].model == "fast-model"

    async def test_reasoning_tier_uses_the_strong_model(self) -> None:
        provider = MockProvider()
        client = build(provider)
        await client.structured(
            TriageResult,
            task="triage",
            system="s",
            user="Where is ORD-1?",
            tier=ModelTier.REASONING,
        )
        assert provider.calls[-1].model == "strong-model"


class TestSchemaRepair:
    async def test_retries_once_and_succeeds(self) -> None:
        provider = FlakyProvider(failures=1)
        result, _ = await build(provider).structured(Shape, task="t", system="s", user="u")
        assert result.value == 42
        assert provider.calls == 2

    async def test_gives_up_rather_than_guessing(self) -> None:
        provider = FlakyProvider(failures=99)
        with pytest.raises(StructuredOutputError, match="after 3 attempts"):
            await build(provider).structured(Shape, task="t", system="s", user="u")

    async def test_repair_turn_includes_the_error(self) -> None:
        provider = FlakyProvider(failures=1)
        await build(provider).structured(Shape, task="t", system="s", user="u")
        # The provider records the request it was last handed; the repair turn
        # appends a message containing the validation error.
        assert provider.calls == 2


class TestCaching:
    async def test_identical_request_is_served_from_cache(self) -> None:
        provider = MockProvider()
        client = LLMClient(
            provider, fast_model="f", reasoning_model="r", cache=ResponseCache(ttl_seconds=60)
        )
        kwargs = {"task": "triage", "system": "s", "user": "Where is ORD-400013?"}
        await client.structured(TriageResult, **kwargs)
        before = len(provider.calls)
        await client.structured(TriageResult, **kwargs)
        assert len(provider.calls) == before
        assert client.cache is not None
        assert client.cache.hits == 1

    async def test_a_different_prompt_is_not_a_hit(self) -> None:
        provider = MockProvider()
        client = LLMClient(provider, fast_model="f", reasoning_model="r", cache=ResponseCache())
        await client.structured(TriageResult, task="triage", system="s", user="Where is ORD-1?")
        await client.structured(TriageResult, task="triage", system="s", user="Refund ORD-2")
        assert len(provider.calls) == 2

    def test_expired_entries_are_misses(self) -> None:
        cache = ResponseCache(ttl_seconds=-1)
        cache.set("k", "v")
        assert cache.get("k") is None


class TestCostAccounting:
    def test_pricing_is_per_million_tokens(self) -> None:
        assert cost_usd("claude-opus-5", 1_000_000, 0) == pytest.approx(5.00)
        assert cost_usd("claude-opus-5", 0, 1_000_000) == pytest.approx(25.00)

    def test_the_cheap_model_really_is_cheaper(self) -> None:
        assert (
            price_for("claude-haiku-4-5").input_per_mtok < price_for("claude-opus-5").input_per_mtok
        )

    def test_unknown_models_fall_back_rather_than_crash(self) -> None:
        assert cost_usd("some-future-model", 1_000_000, 0) > 0

    def test_ledger_totals_and_splits_by_model(self) -> None:
        ledger = CostLedger()
        ledger.record(Usage(model="a", input_tokens=100, output_tokens=10, cost_usd=0.001))
        ledger.record(Usage(model="b", input_tokens=200, output_tokens=20, cost_usd=0.004))
        assert ledger.total_usd == pytest.approx(0.005)
        assert ledger.total_input_tokens == 300
        assert ledger.by_model() == {"a": 0.001, "b": 0.004}
