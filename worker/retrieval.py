"""Hybrid retrieval primitives — dense (Milvus) + sparse (Postgres BM25) + RRF.

Phase 12. ARCHITECTURE.md §2 + §3.5 + ADR 0004. The HTTP wrapper in
``api/search.py`` is a thin async orchestrator; the algorithm lives
here so the worker test surface (``worker/tests/test_retrieval_golden.py``)
can drive the exact same fusion path without going through the API.

## Identity

Both arms agree on the unit of retrieval: a single chunk, identified by
``(book_id, chunk_index)``. The dense arm reads ``chunk_index`` out of
Milvus's per-row metadata (written by ``worker/ingest.py:_build_rows``);
the sparse arm reads it from the ``chunks.chunk_index`` column. RRF
fusion is a dict merge over this key.

## Tenant scoping

Both arms scope to the caller's ``book_id`` set — same JWT-derived list
fetched from Postgres ``user_library`` (CLAUDE.md tenant invariant,
ARCHITECTURE.md §7.1). The dense arm passes it as Milvus
``filter=book_id in [...]``; the sparse arm passes it as SQL
``book_id = ANY(...)``. An empty set is the caller's job to short-circuit
*before* calling either arm — both ``dense_search`` and
``bm25_search_sync`` raise rather than silently returning everything.

## RRF

``rrf_fuse`` implements the standard formula::

    score(d) = sum over arms of  1 / (k + rank(d))

where ``rank`` starts at 1 for the top hit per arm and ``k=60`` per
ARCHITECTURE.md §2 / the Cormack et al. (2009) recommendation. RRF is
rank-based, so the differences in absolute score scales between
COSINE (dense, ``[-1, 1]``) and ``ts_rank_cd`` (sparse, unbounded) wash
out — the only thing that matters per-arm is the *ordering*.

The returned hit's ``score`` is the RRF score (sum of reciprocal ranks)
— not the dense COSINE or sparse rank. Use ``score`` for ordering and
debugging; for absolute relevance, inspect ``dense_score`` /
``sparse_score`` on the hit (preserved when each arm contributed).
"""

# pymilvus 2.6 ships without `py.typed`; same relaxations as the rest of worker/.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnnecessaryComparison=false

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from pymilvus import MilvusClient
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from scripts.bootstrap_milvus import COLLECTION_NAME

# RRF constant from Cormack et al. 2009 + ARCHITECTURE.md §2. 60 is the
# load-bearing magic number; changing it requires re-running goldens.
RRF_K = 60

# How many results to pull from each arm before fusion. 30 is the spec
# from ARCHITECTURE.md §5 (lifecycle pseudocode); larger fan-out widens
# the recall pool at the cost of more bytes shipped from Milvus and
# Postgres per query.
DENSE_FANOUT = 30
SPARSE_FANOUT = 30


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    """One chunk match. ``score`` is RRF after fusion; per-arm scores survive."""

    book_id: uuid.UUID
    chunk_index: int
    content_chunk: str
    metadata: dict[str, Any]
    score: float
    dense_score: float | None = None
    sparse_score: float | None = None


def _build_milvus_filter(book_ids: Sequence[uuid.UUID]) -> str:
    """Build the ``book_id in [...]`` Milvus filter expression.

    UUIDs are inert (hex + dashes only) so f-string interpolation is
    safe; an attacker cannot smuggle filter syntax through a JWT-derived
    ``user_id`` → DB-stored ``book_id``.
    """
    if not book_ids:
        msg = (
            "_build_milvus_filter requires at least one book_id; "
            "caller must short-circuit on empty libraries."
        )
        raise ValueError(msg)
    quoted = ", ".join(f'"{b!s}"' for b in book_ids)
    return f"book_id in [{quoted}]"


def dense_search(
    *,
    client: MilvusClient,
    query_vec: list[float],
    book_ids: Sequence[uuid.UUID],
    limit: int = DENSE_FANOUT,
) -> list[RetrievalHit]:
    """Run a single filtered Milvus COSINE search. Sync; blocking.

    Returns at most *limit* hits, ordered by COSINE similarity
    descending. The ``score`` on each hit is the dense COSINE
    similarity in ``[-1, 1]``; RRF fusion overwrites it later.
    """
    expr = _build_milvus_filter(book_ids)
    results = client.search(
        collection_name=COLLECTION_NAME,
        data=[query_vec],
        filter=expr,
        limit=limit,
        output_fields=["book_id", "content_chunk", "metadata"],
    )
    hits: list[RetrievalHit] = []
    for hit in results[0]:
        entity = hit["entity"]
        metadata = entity["metadata"]
        # chunk_index was written into Milvus metadata in Phase 6 by
        # ingest._build_rows; treat its absence as a bug we should
        # surface rather than silently coerce.
        chunk_index = int(metadata["chunk_index"])
        score = float(hit["distance"])
        hits.append(
            RetrievalHit(
                book_id=uuid.UUID(entity["book_id"]),
                chunk_index=chunk_index,
                content_chunk=entity["content_chunk"],
                metadata=metadata,
                score=score,
                dense_score=score,
            ),
        )
    return hits


# Single SQL spelling — async and sync variants share it. ``websearch_to_tsquery``
# is the safe-for-user-input parser (it ignores malformed operators rather
# than raising). ``ts_rank_cd`` ranks by cover density (Clarke & Cormack
# 2000) — see ADR 0004 for why "BM25" is shorthand here.
_BM25_SQL = text(
    """
    SELECT
        book_id,
        chunk_index,
        content,
        parent_section,
        filename,
        ts_rank_cd(tsv, q) AS rank
      FROM chunks, websearch_to_tsquery('english', :query) AS q
     WHERE tsv @@ q
       AND book_id = ANY(:book_ids)
     ORDER BY rank DESC
     LIMIT :limit
    """,
).bindparams(
    bindparam("book_ids", type_=ARRAY(UUID(as_uuid=True))),
)


def _hit_from_bm25_row(row: Row[Any]) -> RetrievalHit:
    """Shape one Postgres row into a ``RetrievalHit``.

    Metadata mirrors what Milvus carries (``filename``, ``chunk_index``,
    ``parent_section``) so fused hits look the same whichever arm they
    came from.
    """
    book_id, chunk_index, content, parent_section, filename, rank = row
    metadata = {
        "filename": filename,
        "chunk_index": int(chunk_index),
        "parent_section": parent_section,
    }
    score = float(rank)
    return RetrievalHit(
        book_id=book_id,
        chunk_index=int(chunk_index),
        content_chunk=content,
        metadata=metadata,
        score=score,
        sparse_score=score,
    )


def bm25_search_sync(
    *,
    session: Session,
    query: str,
    book_ids: Sequence[uuid.UUID],
    limit: int = SPARSE_FANOUT,
) -> list[RetrievalHit]:
    """Sync Postgres BM25 search. Used by worker tests."""
    if not book_ids:
        msg = (
            "bm25_search_sync requires at least one book_id; "
            "caller must short-circuit on empty libraries."
        )
        raise ValueError(msg)
    result = session.execute(
        _BM25_SQL,
        {"query": query, "book_ids": list(book_ids), "limit": limit},
    )
    return [_hit_from_bm25_row(row) for row in result.all()]


async def bm25_search(
    *,
    session: AsyncSession,
    query: str,
    book_ids: Sequence[uuid.UUID],
    limit: int = SPARSE_FANOUT,
) -> list[RetrievalHit]:
    """Async Postgres BM25 search. Used by the FastAPI handler."""
    if not book_ids:
        msg = (
            "bm25_search requires at least one book_id; "
            "caller must short-circuit on empty libraries."
        )
        raise ValueError(msg)
    result = await session.execute(
        _BM25_SQL,
        {"query": query, "book_ids": list(book_ids), "limit": limit},
    )
    return [_hit_from_bm25_row(row) for row in result.all()]


def rrf_fuse(
    *,
    dense: Sequence[RetrievalHit],
    sparse: Sequence[RetrievalHit],
    limit: int,
    k: int = RRF_K,
) -> list[RetrievalHit]:
    """Reciprocal-rank-fuse two ranked lists by ``(book_id, chunk_index)``.

    Each arm contributes ``1 / (k + rank)`` for the chunks it returned;
    chunks present in both arms get both terms summed. Output is sorted
    by fused score descending, truncated to *limit*.

    The returned hit carries:

    - ``score`` — the RRF score (sum of reciprocal ranks).
    - ``dense_score`` / ``sparse_score`` — per-arm scores when that arm
      contributed, ``None`` otherwise. These let the caller debug a
      ranking shift without re-running the arms.
    """
    scored: dict[tuple[uuid.UUID, int], float] = {}
    representatives: dict[tuple[uuid.UUID, int], RetrievalHit] = {}
    dense_scores: dict[tuple[uuid.UUID, int], float] = {}
    sparse_scores: dict[tuple[uuid.UUID, int], float] = {}

    for rank, hit in enumerate(dense, start=1):
        key = (hit.book_id, hit.chunk_index)
        scored[key] = scored.get(key, 0.0) + 1.0 / (k + rank)
        representatives.setdefault(key, hit)
        # Prefer the dense-side score over a later sparse-side overwrite
        # for the same key — dense provides the canonical content_chunk
        # that came back from Milvus, sparse is the same row from the
        # chunks table either way.
        if hit.dense_score is not None:
            dense_scores[key] = hit.dense_score

    for rank, hit in enumerate(sparse, start=1):
        key = (hit.book_id, hit.chunk_index)
        scored[key] = scored.get(key, 0.0) + 1.0 / (k + rank)
        representatives.setdefault(key, hit)
        if hit.sparse_score is not None:
            sparse_scores[key] = hit.sparse_score

    ranked = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)
    fused: list[RetrievalHit] = []
    for key, rrf_score in ranked[:limit]:
        rep = representatives[key]
        fused.append(
            replace(
                rep,
                score=rrf_score,
                dense_score=dense_scores.get(key),
                sparse_score=sparse_scores.get(key),
            ),
        )
    return fused


def hybrid_search_sync(
    *,
    client: MilvusClient,
    session: Session,
    query: str,
    query_vec: list[float],
    book_ids: Sequence[uuid.UUID],
    limit: int,
) -> list[RetrievalHit]:
    """Run dense + sparse sequentially and fuse. Sync; for worker tests.

    The async handler in ``api/search.py`` runs the two arms in parallel
    via ``asyncio.gather``; the worker-side sync caller pays the
    sequential cost — fine for tests, where wall time is dominated by
    query embedding and Milvus RTT anyway.
    """
    dense = dense_search(
        client=client,
        query_vec=query_vec,
        book_ids=book_ids,
        limit=DENSE_FANOUT,
    )
    sparse = bm25_search_sync(
        session=session,
        query=query,
        book_ids=book_ids,
        limit=SPARSE_FANOUT,
    )
    return rrf_fuse(dense=dense, sparse=sparse, limit=limit)
