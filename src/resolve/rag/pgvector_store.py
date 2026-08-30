"""Production vector store backed by Postgres + pgvector.

Satisfies the same `VectorStore` protocol as `InMemoryVectorStore`, so nothing
above the retrieval layer changes when this is swapped in. The lexical half of
hybrid search moves to Postgres full-text search over the same rows, which
means one datastore rather than a separate search cluster — see ADR-004 for
why that tradeoff is right at this scale.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from resolve.domain.schemas import Chunk

_UPSERT = text(
    """
    INSERT INTO policy_chunks (id, document_id, title, text, ordinal, embedding)
    VALUES (:id, :document_id, :title, :text, :ordinal, :embedding)
    ON CONFLICT (id) DO UPDATE SET
        document_id = EXCLUDED.document_id,
        title       = EXCLUDED.title,
        text        = EXCLUDED.text,
        ordinal     = EXCLUDED.ordinal,
        embedding   = EXCLUDED.embedding
    """
)

# `<=>` is pgvector's cosine-distance operator; 1 - distance is similarity.
_SEARCH = text(
    """
    SELECT id, document_id, title, text, ordinal,
           1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
    FROM policy_chunks
    WHERE embedding IS NOT NULL
    ORDER BY embedding <=> CAST(:embedding AS vector)
    LIMIT :limit
    """
)

_FTS = text(
    """
    SELECT id, ts_rank_cd(to_tsvector('english', text),
                          plainto_tsquery('english', :query)) AS rank
    FROM policy_chunks
    WHERE to_tsvector('english', text) @@ plainto_tsquery('english', :query)
    ORDER BY rank DESC
    LIMIT :limit
    """
)


class PgVectorStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _literal(vector: list[float]) -> str:
        return "[" + ",".join(f"{v:.7f}" for v in vector) + "]"

    async def upsert(self, chunks: list[Chunk]) -> None:
        for chunk in chunks:
            await self.session.execute(
                _UPSERT,
                {
                    "id": chunk.id,
                    "document_id": chunk.document_id,
                    "title": chunk.title,
                    "text": chunk.text,
                    "ordinal": chunk.ordinal,
                    "embedding": self._literal(chunk.embedding) if chunk.embedding else None,
                },
            )
        await self.session.commit()

    async def search(self, embedding: list[float], limit: int) -> list[tuple[Chunk, float]]:
        rows = (
            await self.session.execute(
                _SEARCH, {"embedding": self._literal(embedding), "limit": limit}
            )
        ).mappings()
        return [
            (
                Chunk(
                    id=row["id"],
                    document_id=row["document_id"],
                    title=row["title"],
                    text=row["text"],
                    ordinal=row["ordinal"],
                ),
                float(row["similarity"]),
            )
            for row in rows
        ]

    async def lexical_search(self, query: str, limit: int) -> list[tuple[str, float]]:
        rows = (await self.session.execute(_FTS, {"query": query, "limit": limit})).mappings()
        return [(row["id"], float(row["rank"])) for row in rows]

    async def all_chunks(self) -> list[Chunk]:
        rows = (
            await self.session.execute(
                text("SELECT id, document_id, title, text, ordinal FROM policy_chunks")
            )
        ).mappings()
        return [
            Chunk(
                id=r["id"],
                document_id=r["document_id"],
                title=r["title"],
                text=r["text"],
                ordinal=r["ordinal"],
            )
            for r in rows
        ]
