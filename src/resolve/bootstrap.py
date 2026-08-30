"""Composition root.

One place where concrete implementations are chosen and wired together.
Everything else depends on protocols, which is why the same `AgentRunner`
serves the API, the worker, the CLI demo and the evaluation harness without
any of them knowing whether they are talking to Postgres or a dict, or to
Claude or to the deterministic baseline.
"""

from __future__ import annotations

from dataclasses import dataclass

from resolve.agent.loop import AgentRunner
from resolve.agent.policy import PolicyEngine
from resolve.agent.tools.commerce_tools import build_commerce_registry
from resolve.agent.tools.registry import ToolRegistry
from resolve.config import Settings, get_settings
from resolve.domain.schemas import Chunk, PolicyDocument
from resolve.llm.client import LLMClient, ResponseCache, build_provider
from resolve.rag.chunking import chunk_document
from resolve.rag.embeddings import HashingEmbedder
from resolve.rag.store import HybridRetriever, InMemoryVectorStore
from resolve.seed.generate import Dataset, build_dataset
from resolve.services.commerce import CommerceBackend, InMemoryCommerceBackend


@dataclass
class Application:
    settings: Settings
    dataset: Dataset
    commerce: CommerceBackend
    registry: ToolRegistry
    retriever: HybridRetriever
    llm: LLMClient
    agent: AgentRunner

    @property
    def policy_chunks(self) -> int:
        return len(self.retriever._by_id)


def build_llm_client(settings: Settings) -> LLMClient:
    provider = build_provider(
        settings.llm.provider,
        api_key=settings.llm.api_key,
        base_url=settings.llm.base_url,
        timeout=settings.llm.request_timeout_seconds,
        max_retries=settings.llm.max_retries,
    )
    cache = (
        ResponseCache(ttl_seconds=settings.llm.cache_ttl_seconds)
        if settings.llm.cache_enabled
        else None
    )
    return LLMClient(
        provider,
        fast_model=settings.llm.triage_model,
        reasoning_model=settings.llm.reasoning_model,
        max_retries=settings.llm.max_retries,
        cache=cache,
        max_output_tokens=settings.llm.max_output_tokens,
    )


def chunk_all(documents: list[PolicyDocument]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for doc in documents:
        chunks.extend(chunk_document(doc))
    return chunks


async def build_application(
    settings: Settings | None = None,
    *,
    dataset: Dataset | None = None,
    commerce: CommerceBackend | None = None,
) -> Application:
    settings = settings or get_settings()
    settings.assert_production_safe()

    data = dataset or build_dataset()
    backend = commerce or InMemoryCommerceBackend(orders=data.orders, payments=data.payments)

    retriever = HybridRetriever(InMemoryVectorStore(), HashingEmbedder())
    await retriever.index(chunk_all(data.policies))

    registry = build_commerce_registry(backend)
    policy = PolicyEngine(
        registry=registry,
        refund_auto_approve_limit_gbp=settings.agent.refund_auto_approve_limit_gbp,
        min_confidence_for_auto_action=settings.agent.min_confidence_for_auto_action,
    )
    llm = build_llm_client(settings)

    agent = AgentRunner(
        llm=llm,
        retriever=retriever,
        registry=registry,
        commerce=backend,
        policy=policy,
        settings=settings.agent,
        budget_usd=settings.llm.budget_usd_per_ticket,
        redact_pii=settings.security.redact_pii_before_llm,
    )

    return Application(
        settings=settings,
        dataset=data,
        commerce=backend,
        registry=registry,
        retriever=retriever,
        llm=llm,
        agent=agent,
    )
