"""Retrieval: chunking, hybrid ranking, and the cases each half of the hybrid
exists to cover."""

from __future__ import annotations

import pytest

from resolve.domain.schemas import PolicyDocument
from resolve.rag.chunking import chunk_document
from resolve.rag.embeddings import HashingEmbedder, cosine
from resolve.rag.store import HybridRetriever, InMemoryVectorStore


class TestChunking:
    def test_splits_on_headings_and_carries_them(self) -> None:
        doc = PolicyDocument(
            id="D1",
            title="Test",
            category="test",
            body="# Alpha\nFirst section text.\n\n# Beta\nSecond section text.",
        )
        chunks = chunk_document(doc)
        assert len(chunks) == 2
        assert "Alpha" in chunks[0].text
        assert "Beta" in chunks[1].title

    def test_respects_the_size_ceiling(self) -> None:
        doc = PolicyDocument(
            id="D2",
            title="Long",
            category="test",
            body="# H\n" + "\n\n".join("word " * 60 for _ in range(10)),
        )
        chunks = chunk_document(doc, max_chars=400)
        assert len(chunks) > 1
        assert all(len(c.text) <= 600 for c in chunks)

    def test_chunk_ids_are_stable(self) -> None:
        doc = PolicyDocument(id="D3", title="T", category="c", body="# A\nx\n\n# B\ny")
        assert [c.id for c in chunk_document(doc)] == ["D3#0", "D3#1"]


class TestEmbeddings:
    def test_identical_text_is_identical_vector(self) -> None:
        e = HashingEmbedder(dimensions=128)
        assert e.embed_one("refund policy") == e.embed_one("refund policy")

    def test_similar_text_scores_above_unrelated_text(self) -> None:
        e = HashingEmbedder(dimensions=512)
        refund = e.embed_one("refund returns money back to the customer")
        related = e.embed_one("returns and refunds for customers")
        unrelated = e.embed_one("allergen information for nuts and gluten")
        assert cosine(refund, related) > cosine(refund, unrelated)

    def test_empty_text_is_a_zero_vector(self) -> None:
        assert set(HashingEmbedder(dimensions=32).embed_one("")) == {0.0}


class TestHybridRetrieval:
    @pytest.fixture
    async def retriever(self, dataset) -> HybridRetriever:
        r = HybridRetriever(InMemoryVectorStore(), HashingEmbedder())
        chunks = [c for doc in dataset.policies for c in chunk_document(doc)]
        await r.index(chunks)
        return r

    async def test_paraphrase_finds_the_right_document(self, retriever) -> None:
        """No shared keyword with the heading 'Delayed or missing parcels' —
        this is what the dense half of the hybrid is for."""
        results = await retriever.retrieve("my parcel never showed up", limit=5)
        assert any("DELIVERY" in r.chunk.document_id for r in results)

    async def test_exact_phrase_finds_the_right_document(self, retriever) -> None:
        """'14 days' is a lexical match; BM25 is what finds it."""
        results = await retriever.retrieve("14 days right to cancel", limit=5)
        assert any("REFUNDS" in r.chunk.document_id for r in results)

    async def test_allergen_query_reaches_the_product_policy(self, retriever) -> None:
        results = await retriever.retrieve("does it contain nuts allergy", limit=5)
        assert results[0].chunk.document_id == "POL-PRODUCT"

    async def test_results_are_ordered_by_fused_score(self, retriever) -> None:
        results = await retriever.retrieve("duplicate charge on my card", limit=6)
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

    async def test_empty_query_returns_nothing(self, retriever) -> None:
        assert await retriever.retrieve("   ") == []

    async def test_indexing_is_idempotent(self, retriever, dataset) -> None:
        before = len(await retriever.store.all_chunks())
        await retriever.index([c for doc in dataset.policies for c in chunk_document(doc)])
        assert len(await retriever.store.all_chunks()) == before
