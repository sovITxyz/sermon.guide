"""Authenticated semantic search over the JWT user's library.

``POST /search`` embeds the query with BGE-Large, then runs a Milvus
COSINE search filtered by the user's ``book_id`` set resolved from
``user_library``. Returns the top-K matches as
``{content_chunk, book_id, metadata, score}``.

## Trust boundary

This is the load-bearing tenant invariant for retrieval (repo-root
``CLAUDE.md`` + ``ARCHITECTURE.md`` §3 + §7.1):

- ``user_id`` is **always** ``current_user.user_id`` from the JWT — never
  read from the request body or query. A search payload field named
  ``user_id`` is an automatic reject.
- The ``book_id`` set used in the Milvus filter is resolved server-side
  from ``user_library`` for that JWT ``user_id`` on every request. The
  client cannot widen its own scope by passing ``book_ids: list[UUID]``.
- Every Milvus search includes ``book_id IN (<set>)`` as the filter
  expression. An empty library short-circuits to an empty response
  *before* embedding so we don't run the model on a request that can't
  return anything; we also never issue a ``book_id in []`` filter (some
  pymilvus builds reject it, and the semantics are ambiguous anyway).

## Process-level singletons

BGE-Large is loaded lazily via ``worker.embedding._model`` (an
``@lru_cache``); the first ``/search`` after process boot pays the
~1.3 GB model load + cold inference, every subsequent call is a single
encode. Phase 11 spec calls out "shared embedding loader (don't
duplicate model init across processes)" — importing the same module
satisfies that. The Milvus client is also one per process, lazily
constructed on first use via ``scripts.bootstrap_milvus.make_client``.

Both calls are blocking; we hand them to ``asyncio.to_thread`` so the
async handler doesn't stall the event loop for the ~hundreds-of-ms
encode + the ~tens-of-ms Milvus RTT.

## Why ``score`` and not ``distance``

Milvus 2.6 returns the metric value in the ``distance`` field whether
the metric is a true distance (L2, IP) or a similarity (COSINE). The
collection uses COSINE (``ARCHITECTURE.md`` §3), so the value is
similarity in ``[-1, 1]`` where higher is more similar; calling it
``distance`` in our response would mislead callers. We surface it as
``score`` and document the convention here.
"""

# pymilvus 2.6 ships without `py.typed`; same relaxation as worker/.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnnecessaryComparison=false

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from typing import Any

from db import UserLibraryEntry
from embedding import embed
from fastapi import APIRouter
from pydantic import BaseModel, Field
from pymilvus import MilvusClient
from scripts.bootstrap_milvus import COLLECTION_NAME, make_client
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


def _build_filter_expr(book_ids: Sequence[uuid.UUID]) -> str:
    """Build the ``book_id IN (...)`` expression for a Milvus search filter.

    Milvus stores ``book_id`` as a VARCHAR (the UUID's string form — see
    ``worker/ingest.py``), so the values in the expression are
    double-quoted strings. UUIDs are inert (hex + dashes only) so simple
    f-string interpolation is safe; an attacker cannot smuggle filter
    syntax through a JWT-derived ``user_id`` → DB-stored ``book_id``.
    """
    if not book_ids:
        msg = (
            "_build_filter_expr requires at least one book_id; "
            "caller must short-circuit on empty libraries."
        )
        raise ValueError(msg)
    quoted = ", ".join(f'"{b!s}"' for b in book_ids)
    return f"book_id in [{quoted}]"


def _run_milvus_search(
    *,
    query_vec: list[float],
    book_ids: Sequence[uuid.UUID],
    limit: int,
) -> list[dict[str, Any]]:
    """Run a single filtered Milvus search. Blocking; offload via ``to_thread``.

    Returned hits are pre-shaped for ``SearchHit``: each is a dict with
    ``book_id`` (str), ``content_chunk`` (str), ``metadata`` (dict), and
    ``score`` (COSINE similarity from the ``distance`` field — see module
    docstring on the naming).
    """
    expr = _build_filter_expr(book_ids)
    results = _client().search(
        collection_name=COLLECTION_NAME,
        data=[query_vec],
        filter=expr,
        limit=limit,
        output_fields=["book_id", "content_chunk", "metadata"],
    )
    return [
        {
            "book_id": hit["entity"]["book_id"],
            "content_chunk": hit["entity"]["content_chunk"],
            "metadata": hit["entity"]["metadata"],
            "score": float(hit["distance"]),
        }
        for hit in results[0]
    ]


def _embed_query(query: str) -> list[float]:
    """Embed a single query with BGE-Large. Blocking; offload via ``to_thread``."""
    arr = embed([query])
    return arr[0].tolist()


@router.post("", response_model=SearchResponse)
async def search(
    payload: SearchRequest,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> SearchResponse:
    """Top-K semantic search over the authenticated user's library."""
    stmt = select(UserLibraryEntry.book_id).where(
        UserLibraryEntry.user_id == current_user.user_id,
    )
    book_ids: list[uuid.UUID] = list((await session.execute(stmt)).scalars().all())
    if not book_ids:
        return SearchResponse(hits=[])

    query_vec = await asyncio.to_thread(_embed_query, payload.query)
    raw_hits = await asyncio.to_thread(
        _run_milvus_search,
        query_vec=query_vec,
        book_ids=book_ids,
        limit=payload.limit,
    )
    hits = [
        SearchHit(
            book_id=uuid.UUID(h["book_id"]),
            content_chunk=h["content_chunk"],
            metadata=h["metadata"],
            score=h["score"],
        )
        for h in raw_hits
    ]
    return SearchResponse(hits=hits)
