"""Document chunking.

Policy documents are short, highly structured and heading-delimited, so
paragraph-aware chunking with a size ceiling beats fixed-window splitting:
a chunk that ends mid-sentence retrieves badly and cites worse. The heading
is prepended to every chunk it covers so a chunk retrieved on its own still
carries its context.
"""

from __future__ import annotations

import re

from resolve.domain.schemas import Chunk, PolicyDocument

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+)$", re.MULTILINE)


def _split_sections(body: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, text) sections, preserving order."""
    matches = list(_HEADING.finditer(body))
    if not matches:
        return [("", body.strip())]

    sections: list[tuple[str, str]] = []
    preamble = body[: matches[0].start()].strip()
    if preamble:
        sections.append(("", preamble))

    for i, match in enumerate(matches):
        heading = match.group(1).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        text = body[start:end].strip()
        if text:
            sections.append((heading, text))
    return sections


def chunk_document(
    doc: PolicyDocument,
    *,
    max_chars: int = 900,
    overlap_chars: int = 120,
) -> list[Chunk]:
    """Split a policy document into retrievable, citable chunks."""
    chunks: list[Chunk] = []
    ordinal = 0

    for heading, text in _split_sections(doc.body):
        prefix = f"{heading}\n" if heading else ""
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

        buffer = ""
        for para in paragraphs:
            candidate = f"{buffer}\n\n{para}".strip() if buffer else para
            if len(candidate) <= max_chars:
                buffer = candidate
                continue
            if buffer:
                chunks.append(_make_chunk(doc, prefix + buffer, ordinal, heading))
                ordinal += 1
                tail = buffer[-overlap_chars:] if overlap_chars else ""
                buffer = f"{tail}\n\n{para}".strip() if tail else para
            else:
                # A single paragraph longer than the ceiling: hard-split it.
                for start in range(0, len(para), max_chars):
                    chunks.append(
                        _make_chunk(doc, prefix + para[start : start + max_chars], ordinal, heading)
                    )
                    ordinal += 1
                buffer = ""
        if buffer:
            chunks.append(_make_chunk(doc, prefix + buffer, ordinal, heading))
            ordinal += 1

    return chunks


def _make_chunk(doc: PolicyDocument, text: str, ordinal: int, heading: str) -> Chunk:
    title = f"{doc.title} — {heading}" if heading else doc.title
    return Chunk(
        id=f"{doc.id}#{ordinal}",
        document_id=doc.id,
        title=title,
        text=text.strip(),
        ordinal=ordinal,
    )
