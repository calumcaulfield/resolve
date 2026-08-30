"""Retrieval.

Hybrid search: dense cosine similarity for meaning, BM25 for exact terms.
Neither alone is adequate here. A customer writing "my parcel never showed up"
shares no keyword with a policy headed "Non-delivery claims" — dense retrieval
finds it. A customer quoting an order reference or an exact phrase like
"14 days" needs lexical matching — BM25 finds it, and dense retrieval reliably
does not.

Results are fused with Reciprocal Rank Fusion, which needs no score
normalisation between two incomparable scales and is robust when one retriever
returns nothing useful.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Protocol, runtime_checkable

from resolve.domain.schemas import Chunk, RetrievedChunk
from resolve.rag.embeddings import Embedder, cosine, tokenize


@runtime_checkable
class VectorStore(Protocol):
    async def upsert(self, chunks: list[Chunk]) -> None: ...
    async def search(self, embedding: list[float], limit: int) -> list[tuple[Chunk, float]]: ...
    async def all_chunks(self) -> list[Chunk]: ...


class InMemoryVectorStore:
    """Reference implementation.

    Used by tests, by the evaluation harness and by `make demo`, so none of
    them need Postgres. `PgVectorStore` is the production implementation and
    satisfies the same protocol.
    """

    def __init__(self) -> None:
        self._chunks: dict[str, Chunk] = {}

    async def upsert(self, chunks: list[Chunk]) -> None:
        for chunk in chunks:
            self._chunks[chunk.id] = chunk

    async def search(self, embedding: list[float], limit: int) -> list[tuple[Chunk, float]]:
        scored = [
            (chunk, cosine(embedding, chunk.embedding))
            for chunk in self._chunks.values()
            if chunk.embedding
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]

    async def all_chunks(self) -> list[Chunk]:
        return list(self._chunks.values())

    def __len__(self) -> int:
        return len(self._chunks)


class BM25Index:
    """Okapi BM25 over the same chunk set.

    Small enough to keep in memory for a policy corpus; in production this is
    Postgres full-text search over the same rows, which is why the retriever
    depends on the interface rather than on this class.
    """

    k1 = 1.5
    b = 0.75

    def __init__(self) -> None:
        self._docs: dict[str, list[str]] = {}
        self._df: Counter[str] = Counter()
        self._avg_len = 0.0

    def index(self, chunks: list[Chunk]) -> None:
        for chunk in chunks:
            tokens = tokenize(chunk.text)
            if chunk.id in self._docs:
                for term in set(self._docs[chunk.id]):
                    self._df[term] -= 1
            self._docs[chunk.id] = tokens
            for term in set(tokens):
                self._df[term] += 1
        total = sum(len(t) for t in self._docs.values())
        self._avg_len = total / len(self._docs) if self._docs else 0.0

    def search(self, query: str, limit: int) -> list[tuple[str, float]]:
        if not self._docs:
            return []
        q_terms = tokenize(query)
        n = len(self._docs)
        scores: dict[str, float] = {}

        for chunk_id, tokens in self._docs.items():
            if not tokens:
                continue
            tf = Counter(tokens)
            length = len(tokens)
            score = 0.0
            for term in q_terms:
                freq = tf.get(term, 0)
                if freq == 0:
                    continue
                df = max(1, self._df.get(term, 0))
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                denom = freq + self.k1 * (1 - self.b + self.b * length / (self._avg_len or 1))
                score += idf * (freq * (self.k1 + 1)) / denom
            if score > 0:
                scores[chunk_id] = score

        ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
        return ranked[:limit]


class HybridRetriever:
    """Dense + lexical retrieval fused by Reciprocal Rank Fusion."""

    def __init__(
        self,
        store: VectorStore,
        embedder: Embedder,
        *,
        rrf_k: int = 60,
        candidate_multiplier: int = 3,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.rrf_k = rrf_k
        self.candidate_multiplier = candidate_multiplier
        self.bm25 = BM25Index()
        self._by_id: dict[str, Chunk] = {}

    async def index(self, chunks: list[Chunk]) -> None:
        """Embed and index chunks. Idempotent."""
        to_embed = [c for c in chunks if not c.embedding]
        if to_embed:
            vectors = await self.embedder.embed([c.text for c in to_embed])
            for chunk, vector in zip(to_embed, vectors, strict=True):
                chunk.embedding = vector
        await self.store.upsert(chunks)
        self.bm25.index(chunks)
        for chunk in chunks:
            self._by_id[chunk.id] = chunk

    async def retrieve(self, query: str, limit: int = 6) -> list[RetrievedChunk]:
        if not query.strip():
            return []

        candidates = max(limit * self.candidate_multiplier, limit)
        query_vector = (await self.embedder.embed([query]))[0]

        dense = await self.store.search(query_vector, candidates)
        lexical = self.bm25.search(query, candidates)

        dense_rank = {chunk.id: i for i, (chunk, _) in enumerate(dense)}
        dense_score = {chunk.id: score for chunk, score in dense}
        lexical_rank = {cid: i for i, (cid, _) in enumerate(lexical)}
        lexical_score = dict(lexical)

        fused: dict[str, float] = {}
        for cid, rank in dense_rank.items():
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (self.rrf_k + rank + 1)
        for cid, rank in lexical_rank.items():
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (self.rrf_k + rank + 1)

        for dense_chunk, _ in dense:
            self._by_id.setdefault(dense_chunk.id, dense_chunk)

        ranked = sorted(fused.items(), key=lambda pair: pair[1], reverse=True)[:limit]
        results: list[RetrievedChunk] = []
        for cid, score in ranked:
            chunk = self._by_id.get(cid)
            if chunk is None:
                continue
            results.append(
                RetrievedChunk(
                    chunk=chunk,
                    semantic_score=round(dense_score.get(cid, 0.0), 6),
                    lexical_score=round(lexical_score.get(cid, 0.0), 6),
                    score=round(score, 6),
                )
            )
        return results
