"""Embeddings.

The default embedder is local, deterministic and keyless — a hashed
bag-of-n-grams projected into a fixed-dimension space and L2-normalised, so
cosine similarity is meaningful and repeatable. For a corpus of a few hundred
short policy chunks with a domain-specific vocabulary this retrieves well, and
it keeps the "clone and run" promise intact (ADR-002, ADR-004).

`OpenAIEmbedder` is provided for the case where semantic generalisation across
paraphrase actually matters; it satisfies the same protocol, so swapping is a
configuration change.
"""

from __future__ import annotations

import hashlib
import math
import re
from itertools import pairwise
from typing import Protocol, runtime_checkable

_TOKEN = re.compile(r"[a-z0-9']+")

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "out",
        "over",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "too",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1]


@runtime_checkable
class Embedder(Protocol):
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Feature-hashing embedder over unigrams and bigrams.

    Sub-linear term weighting (1 + log tf) damps repetition, and signed
    hashing keeps collisions from systematically inflating similarity.
    """

    def __init__(self, dimensions: int = 512) -> None:
        self.dimensions = dimensions

    def _hash(self, token: str) -> tuple[int, float]:
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % self.dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        return index, sign

    def embed_one(self, text: str) -> list[float]:
        tokens = tokenize(text)
        if not tokens:
            return [0.0] * self.dimensions

        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        for a, b in pairwise(tokens):
            bigram = f"{a}_{b}"
            counts[bigram] = counts.get(bigram, 0) + 1

        vector = [0.0] * self.dimensions
        for token, count in counts.items():
            index, sign = self._hash(token)
            vector[index] += sign * (1.0 + math.log(count))

        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            return vector
        return [v / norm for v in vector]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


class OpenAIEmbedder:  # pragma: no cover - requires network + key
    """Optional remote embedder. Same protocol, different tradeoff."""

    def __init__(self, model: str = "text-embedding-3-small", dimensions: int = 1536) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError("pip install 'resolve[openai]' to use OpenAIEmbedder") from exc
        self._client = AsyncOpenAI()
        self.model = model
        self.dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        response = await self._client.embeddings.create(model=self.model, input=texts)
        return [item.embedding for item in response.data]


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"dimension mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    # Vectors from HashingEmbedder are already unit length; guard anyway so
    # this function is correct for any caller.
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
