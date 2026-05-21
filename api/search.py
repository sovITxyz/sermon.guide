"""Authenticated hybrid search over the JWT user's library.

``POST /search`` runs dense (BGE → Milvus COSINE) and sparse
(Postgres ``tsvector`` → ``ts_rank_cd``) retrieval in parallel and
fuses them via Reciprocal Rank Fusion (RRF, k=60). Returns the top-K
fused hits as ``{content_chunk, book_id, metadata, score}`` —
``score`` is the RRF score (sum of reciprocal ranks across arms).

Algorithm and fusion live in ``worker/retrieval.py``; this module is
the FastAPI wrapper that handles auth, request validation, and the
event-loop dance between sync (Milvus, BGE encode) and async
(SQLAlchemy ``AsyncSession``) call sites.

## Trust boundary

This is the load-bearing tenant invariant for retrieval (repo-root
``CLAUDE.md`` + ``ARCHITECTURE.md`` §3 + §7.1):

- ``user_id`` is **always** ``current_user.user_id`` from the JWT — never
  read from the request body or query. A search payload field named
  ``user_id`` is an automatic reject.
- The ``book_id`` set used by both arms is resolved server-side from
  ``user_library`` for that JWT ``user_id`` on every request. The
  client cannot widen its own scope by passing ``book_ids: list[UUID]``.
- Every Milvus search includes ``book_id IN (<set>)`` as the filter
  expression; every BM25 search includes ``book_id = ANY(<set>)`` in
  the WHERE clause. An empty library short-circuits to an empty response
  *before* embedding so we don't run the model on a request that can't
  return anything; we also never issue a ``book_id in []`` filter (some
  pymilvus builds reject it, and the semantics are ambiguous anyway).

## Parallelism

The async handler kicks off two concurrent tasks via ``asyncio.gather``:

1. Embed the query (``asyncio.to_thread`` around the BGE encode), then
   run the Milvus search (also ``to_thread`` — pymilvus is blocking).
2. Run the BM25 search directly on the request's ``AsyncSession``.

The dense arm is sequential within itself because the Milvus call needs
the embedded query vector; the two arms run concurrently against each
other. Total wall time is roughly ``max(embed + milvus, bm25)``.

## Process-level singletons

BGE-Large is loaded lazily via ``worker.embedding._model`` (``@lru_cache``);
the first ``/search`` after process boot pays the ~1.3 GB model load +
cold inference, every subsequent call is a single encode. The Milvus
client is also one per process, lazily constructed on first use via
``scripts.bootstrap_milvus.make_client``.

## Why ``score`` is RRF and not COSINE

Phase 11 surfaced ``score`` as the Milvus COSINE similarity. With
fusion, the field semantics change to the RRF score (sum of reciprocal
ranks). The per-arm scores survive on each hit as ``dense_score`` and
``sparse_score`` for debugging, but they are not in the public
``SearchHit`` schema — clients see only the fused order.
"""

# pymilvus 2.6 ships without `py.typed`; same relaxation as worker/.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnnecessaryComparison=false

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from db import UserLibraryEntry
from embedding import embed
from fastapi import APIRouter
from pydantic import BaseModel, Field
from pymilvus import MilvusClient
from retrieval import (
    DENSE_FANOUT,
    SPARSE_FANOUT,
    RetrievalHit,
    bm25_search,
    dense_search,
    rrf_fuse,
)
from scripts.bootstrap_milvus import make_client
from sqlalchemy import select

from auth import CurrentUserDep, SessionDep

router = APIRouter(prefix="/search", tags=["search"])

# Process-wide Milvus client. Lazily constructed so import-time doesn't
# require Milvus to be reachable (tests / type-check / lint shouldn't
# need a live broker).
_milvus_client: MilvusClient | None = None


def _client() -> MilvusClient:
    global _milvus_client  # noqa: PLW0603 — module-level singleton, see module docstring
    if _milvus_client is None:
        _milvus_client = make_client()
    return _milvus_client


class SearchRequest(BaseModel):
    """Search payload.

    No ``user_id`` field — that always comes from the JWT (see module docstring).
    No ``book_ids`` field — the library is resolved server-side.
    """

    query: str = Field(min_length=1, max_length=1024)
    limit: int = Field(default=10, ge=1, le=100)


class SearchHit(BaseModel):
    book_id: uuid.UUID
    content_chunk: str
    metadata: dict[str, Any]
    score: float


class SearchResponse(BaseModel):
    hits: list[SearchHit]


def _embed_query(query: str) -> list[float]:
    """Embed a single query with BGE-Large. Blocking; offload via ``to_thread``."""
    arr = embed([query])
    return arr[0].tolist()


async def _dense_arm(query: str, book_ids: list[uuid.UUID]) -> list[RetrievalHit]:
    """Embed the query + run the Milvus search, both off the event loop."""
    query_vec = await asyncio.to_thread(_embed_query, query)
    return await asyncio.to_thread(
        dense_search,
        client=_client(),
        query_vec=query_vec,
        book_ids=book_ids,
        limit=DENSE_FANOUT,
    )


def _to_search_hit(hit: RetrievalHit) -> SearchHit:
    return SearchHit(
        book_id=hit.book_id,
        content_chunk=hit.content_chunk,
        metadata=hit.metadata,
        score=hit.score,
    )


@router.post("", response_model=SearchResponse)
async def search(
    payload: SearchRequest,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> SearchResponse:
    """Top-K hybrid search over the authenticated user's library."""
    stmt = select(UserLibraryEntry.book_id).where(
        UserLibraryEntry.user_id == current_user.user_id,
    )
    book_ids: list[uuid.UUID] = list((await session.execute(stmt)).scalars().all())
    if not book_ids:
        return SearchResponse(hits=[])

    dense_hits, sparse_hits = await asyncio.gather(
        _dense_arm(payload.query, book_ids),
        bm25_search(
            session=session,
            query=payload.query,
            book_ids=book_ids,
            limit=SPARSE_FANOUT,
        ),
    )
    fused = rrf_fuse(dense=dense_hits, sparse=sparse_hits, limit=payload.limit)
    return SearchResponse(hits=[_to_search_hit(h) for h in fused])
