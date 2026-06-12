"""Reader routes — windowed chunk pages + per-(user, book) reading positions.

Phase 32 (B1 groundwork): the web reader (Phase 33) pages through a
book's ``chunks`` rows in ``chunk_index`` order and persists a resume
point per (user, book) in ``reading_positions`` (migration 0005).

- ``GET /books/{book_id}/chunks?start&limit`` — a window of chunk text,
  ``chunk_index`` ascending. ``start`` is a chunk_index lower bound
  (chunk_index is dense ``0..N-1`` per book, so this equals an OFFSET —
  but index-anchored reads serve the Phase 33 ``?chunk=N`` deep-link
  directly and hit ``uq_chunks_book_chunk``). A ``start`` past the end
  is an empty list, not an error. ``limit`` defaults to 40 and is
  silently capped at 100 (the Phase 32 contract: ``?limit=500`` returns
  100 rows, not a 422); a non-positive ``limit`` or negative ``start``
  is malformed input and 422s per the Phase 18 fail-loud posture.
- ``GET /books/{book_id}/position`` — the saved position, or an
  all-null shape when none exists yet (the ``TaskStatusResponse``
  nullable-``result`` precedent; 404 is reserved for the ownership gate
  so "no position yet" can never be confused with "not your book").
- ``PUT /books/{book_id}/position`` — full-replace upsert ON CONFLICT
  against ``uq_reading_positions_user_book``. An omitted
  ``offset_ratio`` stores NULL — the PUT states the whole position, it
  is not a patch (a stale ratio from a previous chunk must not
  survive). ``updated_at`` is bumped explicitly in the upsert (the
  column has ``server_default=now()`` but no ``onupdate``).

## Tenant gate (load-bearing)

``chunks`` is shared-by-design — it has NO ``user_id`` column;
``user_library`` membership IS the tenant gate (repo-root ``CLAUDE.md``,
ARCHITECTURE.md §4). Every route here resolves the gate FIRST via
``_membership_stmt`` (both predicates from ``_require_owned_book``:
``book_id`` from the path, ``user_id`` ALWAYS from the JWT) and
collapses every failure — non-UUID garbage, nonexistent book, another
tenant's book — into one identical 404, the ``uploads.py``
``GET /tasks/{task_id}`` no-existence-oracle contract. Only after the
gate passes does any chunk or position query run.

``reading_positions`` rows are doubly-scoped like ``highlights``: every
query filters by BOTH the JWT ``user_id`` and ``book_id``
(``_position_stmt`` / ``_position_upsert_stmt`` — compile-pinned in
``tests/test_reader_unit.py``). ``PositionUpdate`` sets
``extra="forbid"`` so a smuggled ``user_id`` is a hard 422 (Phase 18).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from db import Chunk, ReadingPosition, UserLibraryEntry
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import ReturningInsert

from auth import CurrentUserDep, SessionDep

router = APIRouter(prefix="/books", tags=["reader"])

# Window sizing (Phase 32 contract): default 40 chunks per page, hard cap
# 100 — an over-ask is capped, never an error (see the module docstring).
DEFAULT_WINDOW = 40
MAX_WINDOW = 100


class ChunkItem(BaseModel):
    chunk_index: int
    content: str


class ChunkWindowResponse(BaseModel):
    book_id: uuid.UUID
    chunks: list[ChunkItem]


class PositionResponse(BaseModel):
    """Saved position, or all-null fields when the user has none yet."""

    book_id: uuid.UUID
    chunk_index: int | None = None
    offset_ratio: float | None = None
    updated_at: datetime | None = None


class PositionUpdate(BaseModel):
    """PUT body. No ``user_id``/``book_id`` fields — JWT + path only.

    ``extra="forbid"`` (Phase 18 posture): a smuggled ``user_id`` is a
    hard 422, never a silently-dropped key. ``offset_ratio`` optionally
    refines ``chunk_index`` to a 0.0–1.0 scroll position within that
    chunk; the range is validated here because the column deliberately
    has no DB CHECK (worker/db schema-only rule).
    """

    model_config = ConfigDict(extra="forbid")

    chunk_index: int = Field(ge=0)
    offset_ratio: float | None = Field(default=None, ge=0.0, le=1.0)


def _membership_stmt(book_id: uuid.UUID, user_id: uuid.UUID) -> Select[tuple[uuid.UUID]]:
    """Build the tenant gate: is *book_id* in *user_id*'s library?

    Factored out so BOTH predicates can be compile-pinned in a unit test
    (the ``uploads._ownership_stmt`` pattern). Drop ``user_id`` and any
    authenticated user can read any ingested book — ``chunks`` has no
    ``user_id`` column, so this statement is the ONLY tenant gate on the
    reader surface. ``user_id`` is ALWAYS the JWT-derived value.
    """
    return select(UserLibraryEntry.book_id).where(
        UserLibraryEntry.book_id == book_id,
        UserLibraryEntry.user_id == user_id,
    )


def _chunk_window_stmt(
    book_id: uuid.UUID,
    *,
    start: int,
    limit: int,
) -> Select[tuple[int, str]]:
    """Build the chunk window: ``chunk_index >= start``, ascending, LIMIT.

    Runs ONLY after ``_membership_stmt`` passes (chunks is shared across
    tenants by design — no user_id predicate exists to add here). The
    ``(book_id, chunk_index)`` predicates ride ``uq_chunks_book_chunk``.
    """
    return (
        select(Chunk.chunk_index, Chunk.content)
        .where(Chunk.book_id == book_id, Chunk.chunk_index >= start)
        .order_by(Chunk.chunk_index.asc())
        .limit(limit)
    )


def _position_stmt(
    book_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Select[tuple[int, float | None, datetime]]:
    """Build the doubly-scoped position lookup (user_id AND book_id).

    Both predicates are load-bearing (the ``highlights`` invariant):
    drop ``user_id`` and a shared deduped book serves another tenant's
    resume point.
    """
    return select(
        ReadingPosition.chunk_index,
        ReadingPosition.offset_ratio,
        ReadingPosition.updated_at,
    ).where(
        ReadingPosition.book_id == book_id,
        ReadingPosition.user_id == user_id,
    )


def _position_upsert_stmt(
    *,
    user_id: uuid.UUID,
    book_id: uuid.UUID,
    chunk_index: int,
    offset_ratio: float | None,
) -> ReturningInsert[tuple[int, float | None, datetime]]:
    """Build the position upsert: INSERT … ON CONFLICT (user, book) DO UPDATE.

    The conflict target is ``uq_reading_positions_user_book`` — one row
    per (user, book), so a PUT twice updates in place. ``updated_at`` is
    set explicitly (the column has ``server_default`` but no
    ``onupdate``); ``offset_ratio`` is replaced wholesale, NULL included
    (full-replace semantics, see the module docstring). ``user_id`` is
    ALWAYS the JWT-derived value, never the request body.
    """
    stmt = pg_insert(ReadingPosition).values(
        user_id=user_id,
        book_id=book_id,
        chunk_index=chunk_index,
        offset_ratio=offset_ratio,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_reading_positions_user_book",
        set_={
            "chunk_index": stmt.excluded.chunk_index,
            "offset_ratio": stmt.excluded.offset_ratio,
            "updated_at": func.now(),
        },
    )
    return stmt.returning(
        ReadingPosition.chunk_index,
        ReadingPosition.offset_ratio,
        ReadingPosition.updated_at,
    )


async def _require_owned_book(
    book_id: str,
    user_id: uuid.UUID,
    session: AsyncSession,
) -> uuid.UUID:
    """404 unless *book_id* parses AND is in *user_id*'s library.

    Non-UUID garbage, nonexistent ids, and another tenant's books are
    byte-identical 404s — no existence oracle (the ``uploads.py``
    ``GET /tasks/{task_id}`` contract). Returns the parsed UUID so
    callers query with a value that provably passed the gate.
    """
    not_found = HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Book not found.",
    )
    try:
        book_uuid = uuid.UUID(book_id)
    except ValueError as exc:
        # Not a UUID → cannot be a global_books PK → same 404 shape.
        raise not_found from exc
    owned = await session.execute(_membership_stmt(book_uuid, user_id))
    if owned.scalar_one_or_none() is None:
        raise not_found
    return book_uuid


@router.get("/{book_id}/chunks", response_model=ChunkWindowResponse)
async def get_chunk_window(
    book_id: str,
    current_user: CurrentUserDep,
    session: SessionDep,
    start: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1)] = DEFAULT_WINDOW,
) -> ChunkWindowResponse:
    """A window of the book's chunks, ``chunk_index`` ascending.

    404 unless the book is in the JWT user's library (the only tenant
    gate — see the module docstring). ``limit`` over 100 is capped, a
    ``start`` past the end is an empty list with 200.
    """
    book_uuid = await _require_owned_book(book_id, current_user.user_id, session)
    window = min(limit, MAX_WINDOW)
    result = await session.execute(_chunk_window_stmt(book_uuid, start=start, limit=window))
    chunks = [
        ChunkItem(chunk_index=chunk_index, content=content)
        for chunk_index, content in result.tuples().all()
    ]
    return ChunkWindowResponse(book_id=book_uuid, chunks=chunks)


@router.get("/{book_id}/position", response_model=PositionResponse)
async def get_position(
    book_id: str,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> PositionResponse:
    """The caller's saved position for this book; null fields when none."""
    book_uuid = await _require_owned_book(book_id, current_user.user_id, session)
    result = await session.execute(_position_stmt(book_uuid, current_user.user_id))
    row = result.tuples().one_or_none()
    if row is None:
        return PositionResponse(book_id=book_uuid)
    chunk_index, offset_ratio, updated_at = row
    return PositionResponse(
        book_id=book_uuid,
        chunk_index=chunk_index,
        offset_ratio=offset_ratio,
        updated_at=updated_at,
    )


@router.put("/{book_id}/position", response_model=PositionResponse)
async def put_position(
    book_id: str,
    payload: PositionUpdate,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> PositionResponse:
    """Upsert the caller's position — one row per (user, book), replaced whole."""
    book_uuid = await _require_owned_book(book_id, current_user.user_id, session)
    stmt = _position_upsert_stmt(
        user_id=current_user.user_id,
        book_id=book_uuid,
        chunk_index=payload.chunk_index,
        offset_ratio=payload.offset_ratio,
    )
    row = (await session.execute(stmt)).tuples().one()
    await session.commit()
    chunk_index, offset_ratio, updated_at = row
    return PositionResponse(
        book_id=book_uuid,
        chunk_index=chunk_index,
        offset_ratio=offset_ratio,
        updated_at=updated_at,
    )
