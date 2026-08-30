"""Model pricing, in USD per million tokens.

Kept in one place so the cost ledger is auditable and so a price change is a
one-line diff rather than a hunt. Prices are list prices at the time of
writing and are used only to produce the cost figures this project reports;
they are not a billing system.
"""

from __future__ import annotations

from typing import NamedTuple


class Price(NamedTuple):
    input_per_mtok: float
    output_per_mtok: float


PRICING: dict[str, Price] = {
    # Anthropic
    "claude-opus-5": Price(5.00, 25.00),
    "claude-sonnet-5": Price(3.00, 15.00),
    "claude-haiku-4-5": Price(1.00, 5.00),
    # OpenAI (approximate list prices; used only for comparison runs)
    "gpt-4o": Price(2.50, 10.00),
    "gpt-4o-mini": Price(0.15, 0.60),
    # Local deterministic provider — free by construction.
    "mock-deterministic": Price(0.0, 0.0),
}

_FALLBACK = Price(3.00, 15.00)


def price_for(model: str) -> Price:
    if model in PRICING:
        return PRICING[model]
    # Prefix match so dated snapshots resolve to their family price.
    for known, price in PRICING.items():
        if model.startswith(known):
            return price
    return _FALLBACK


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    p = price_for(model)
    return round(
        (input_tokens / 1_000_000) * p.input_per_mtok
        + (output_tokens / 1_000_000) * p.output_per_mtok,
        8,
    )


def estimate_tokens(text: str) -> int:
    """Rough token estimate used only by the deterministic provider.

    Real providers report exact counts; this exists so offline runs still
    produce a plausible, internally-consistent cost model. ~4 chars/token.
    """
    return max(1, len(text) // 4)
