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

import os
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

from scripts.bootstrap_milvus import COLLECTION_NAME, MILVUS_TIMEOUT_SECONDS

# RRF constant from Cormack et al. 2009 + ARCHITECTURE.md §2. 60 is the
# load-bearing magic number; changing it requires re-running goldens.
RRF_K = 60

# How many results to pull from each arm before fusion. 30 is the spec
# from ARCHITECTURE.md §5 (lifecycle pseudocode); larger fan-out widens
# the recall pool at the cost of more bytes shipped from Milvus and
# Postgres per query.
DENSE_FANOUT = 30
SPARSE_FANOUT = 30


def _default_book_id_chunk() -> int:
    """Resolve the per-search ``book_id`` filter chunk size (Phase 24).

    Overridable via ``SERMON_MILVUS_FILTER_BOOK_ID_CHUNK`` so an operator
    can tune the trade-off without a code change (mirrors the ``SERMON_*``
    env convention in ``db/settings.py`` / ``bootstrap_milvus.py``). A
    non-positive or non-integer value falls back to the default rather
    than silently disabling chunking — disabling it reintroduces the
    unbounded-``expr`` problem this constant exists to bound.
    """
    raw = os.environ.get("SERMON_MILVUS_FILTER_BOOK_ID_CHUNK")
    if raw is None:
        return _MILVUS_FILTER_BOOK_ID_CHUNK_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return _MILVUS_FILTER_BOOK_ID_CHUNK_DEFAULT
    return value if value > 0 else _MILVUS_FILTER_BOOK_ID_CHUNK_DEFAULT


# Cap on how many ``book_id``s go into a single Milvus ``book_id in [...]``
# filter expression (Phase 24). A 10K-book library at ~36 bytes/UUID-in-
# expr is a ~360 KB string per search; splitting it into ≤1000-book chunks
# keeps every expr ~36 KB while preserving FULL recall — each chunk runs
# its own scoped search and the per-chunk hits merge back into the global
# top-K. A silent cap (dropping books past the first N) would exclude part
# of a user's library — both a correctness regression AND a tenant-trust
# regression — so chunking, not truncation, is the only acceptable fix.
_MILVUS_FILTER_BOOK_ID_CHUNK_DEFAULT = 1000
MILVUS_FILTER_BOOK_ID_CHUNK = _default_book_id_chunk()


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


def _dense_search_one_chunk(
    *,
    client: MilvusClient,
    query_vec: list[float],
    book_ids: Sequence[uuid.UUID],
    limit: int,
) -> list[RetrievalHit]:
    """Run ONE filtered Milvus COSINE search over a single ``book_id`` chunk.

    Always scoped — the ``filter=`` expression is mandatory and built
    from *book_ids*; this never issues an unfiltered search. This is the
    tenant-isolation boundary: the caller guarantees *book_ids* is a
    subset of the JWT-derived library, so each chunk's expr is too.
    """
    expr = _build_milvus_filter(book_ids)
    results = client.search(
        collection_name=COLLECTION_NAME,
        data=[query_vec],
        filter=expr,
        limit=limit,
        output_fields=["book_id", "content_chunk", "metadata"],
        timeout=MILVUS_TIMEOUT_SECONDS,
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


def dense_search(
    *,
    client: MilvusClient,
    query_vec: list[float],
    book_ids: Sequence[uuid.UUID],
    limit: int = DENSE_FANOUT,
    chunk_size: int = MILVUS_FILTER_BOOK_ID_CHUNK,
) -> list[RetrievalHit]:
    """Run a filtered Milvus COSINE search, chunking the filter (Phase 24).

    Returns at most *limit* hits, ordered by COSINE similarity
    descending. The ``score`` on each hit is the dense COSINE
    similarity in ``[-1, 1]``; RRF fusion overwrites it later.

    ## Filter chunking (Phase 24)

    When ``len(book_ids) <= chunk_size`` this is behaviorally IDENTICAL
    to the pre-Phase-24 single search: one ``client.search`` with one
    ``book_id in [...]`` expr. When the library is larger, the
    ``book_id`` set is split into ``chunk_size``-book slices and one
    scoped search runs per slice (each with ``limit=limit`` so the global
    top-*limit* can be recovered no matter how the best hits are
    distributed across slices). The per-slice hits are merged by COSINE
    distance descending and truncated to *limit*, so FULL recall over the
    whole library is preserved — no book is silently dropped, which would
    be both a correctness AND a tenant-trust regression. The union of the
    per-slice filters equals exactly *book_ids*: ``itertools``-free,
    contiguous, non-overlapping slices of the input list.

    ## Timeout

    The per-call ``timeout`` (Phase 22) is pymilvus's retry budget for
    the search RPC. It bounds the steady-state-down case (a closed/dead
    channel fast-fails as a typed ``MilvusException`` in roughly
    0.4-1.2 s) and keeps retries from compounding — but it is NOT a hard
    wall-clock ceiling: on a warm connection's FIRST failure pymilvus 2.6
    calls its connection-recovery hook BEFORE the deadline check, and
    that hook runs an in-request reconnect with a hardcoded 10 s
    channel-ready wait (live-measured at ~10-11 s before the exception
    surfaces). Callers that need a hard per-request bound must enforce it
    outside the RPC: ``api/search.py`` wraps this call (plus client
    checkout) in ``asyncio.wait_for`` under ``DENSE_ARM_BUDGET_SECONDS``
    — the ``wait_for`` cannot cancel the blocking thread, it abandons it,
    so the orphaned thread drains within pymilvus's own 10 s ceiling. The
    chunked path issues each slice's search sequentially under that same
    single outer budget; a large library therefore costs proportionally
    more wall time, bounded by the caller's budget (a slice that trips the
    budget raises ``MilvusException``/``TimeoutError`` and degrades the
    whole arm, never returns a partial-library result that would look like
    a silent cap).
    """
    # ``_build_milvus_filter`` (per chunk) keeps the empty-library
    # ValueError guard: an empty input yields zero chunks, so guard here
    # too rather than silently returning [] (a no-hit result is
    # indistinguishable from a missed short-circuit).
    if not book_ids:
        msg = (
            "dense_search requires at least one book_id; "
            "caller must short-circuit on empty libraries."
        )
        raise ValueError(msg)

    # Fast path: small library → one search, byte-for-byte the same expr
    # and call shape as before Phase 24.
    if len(book_ids) <= chunk_size:
        return _dense_search_one_chunk(
            client=client,
            query_vec=query_vec,
            book_ids=book_ids,
            limit=limit,
        )

    # Chunked path: contiguous, non-overlapping slices whose union is
    # exactly ``book_ids`` (no book added, none dropped → the tenant
    # boundary is preserved). Each slice pulls its own top-``limit`` so
    # the global top-``limit`` survives any distribution of best hits
    # across slices.
    merged: list[RetrievalHit] = []
    for start in range(0, len(book_ids), chunk_size):
        slice_ids = book_ids[start : start + chunk_size]
        merged.extend(
            _dense_search_one_chunk(
                client=client,
                query_vec=query_vec,
                book_ids=slice_ids,
                limit=limit,
            ),
        )
    # Merge per-slice hits into the global top-``limit`` by COSINE
    # distance descending. ``dense_score`` is the raw COSINE distance
    # (set in ``_dense_search_one_chunk``); a chunk boundary never splits
    # a single (book_id, chunk_index) across slices, so no dedup is
    # needed — book_ids are disjoint across slices.
    merged.sort(key=lambda h: h.score, reverse=True)
    return merged[:limit]


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
