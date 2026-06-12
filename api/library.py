"""User library listing route.

``GET /library`` returns the authenticated user's books — every
``user_library`` row for the JWT-derived ``user_id`` joined to its shared
``global_books`` row for display metadata (title, author), newest first.
The web ``/library`` page (Phase 15) renders these as a table.

Phase 32 adds per-book reading progress: each row carries the book's
``chunk_count`` (computed per request via a GROUP BY subquery over
``chunks`` — the in-phase decision was NOT to denormalize onto
``global_books``), the caller's ``last_chunk_index`` from
``reading_positions``, and a derived ``progress`` ratio
(``(last_chunk_index + 1) / chunk_count``, clamped to 1.0 — "chunks
completed over total"; the raw fields ride along so the web tier can
refine with ``offset_ratio`` from ``GET /books/{id}/position`` if it
ever wants to). All three are ``None`` when the user has no saved
position (or, for ``chunk_count``, when the book has no chunks).

Tenant invariant (repo-root ``CLAUDE.md``): the ``user_id`` is ALWAYS
``current_user.user_id`` (the JWT ``sub``); the listing is filtered by it
server-side. ``global_books`` is shared-by-design (ARCHITECTURE.md §3/§4) —
a title is not user-scoped — so the inner join adds no tenant surface; the
``where(user_id == ...)`` on ``user_library`` is the only gate, and there is
no client-supplied ``user_id`` / ``book_id`` anywhere on this path.

THE PHASE 32 TRAP (B1, verbatim): the join to ``reading_positions`` MUST
be ON (user_id AND book_id) — joining on ``book_id`` alone leaks another
tenant's reading position for a shared deduped book. The two-column ON
clause is compile-pinned in ``tests/test_library_unit.py``. The
``chunks`` count subquery joins on ``book_id`` alone BY DESIGN: a chunk
count is a property of the shared deduped book (like its title), not of
any tenant.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import cast

from db import Chunk, GlobalBook, ReadingPosition, UserLibraryEntry
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import ColumnElement, Select, func, select

from auth import CurrentUserDep, SessionDep

router = APIRouter(prefix="/library", tags=["library"])


class LibraryBook(BaseModel):
    book_id: uuid.UUID
    title: str
    author: str | None = None
    category: str | None = None
    added_at: datetime
    # Phase 32 progress fields — optional with None defaults so the shape
    # stays backward-compatible for the Phase 15 web table.
    chunk_count: int | None = None
    last_chunk_index: int | None = None
    progress: float | None = None


class LibraryResponse(BaseModel):
    books: list[LibraryBook]


def _library_stmt(
    user_id: uuid.UUID,
) -> Select[tuple[uuid.UUID, str, str | None, str | None, datetime, int | None, int | None]]:
    """Build the tenant-scoped library query for *user_id*.

    Factored out so the tenant filter can be asserted in a unit test
    without a live database (``tests/test_library_unit.py``). The WHERE
    clause is the load-bearing line: drop it and every user sees every
    user's library. The ``reading_positions`` LEFT JOIN is equally
    load-bearing: its ON clause carries BOTH ``user_id`` and ``book_id``
    (the module-docstring trap) — both joins are pinned in the same test.
    """
    chunk_counts = (
        select(Chunk.book_id, func.count().label("chunk_count")).group_by(Chunk.book_id).subquery()
    )
    # Both LEFT JOINs make these columns NULL-able at runtime; SQLAlchemy's
    # typing can't express outer-join nullability (and subquery `.c` columns
    # are Any), so cast to the honest SQL types.
    chunk_count_col = cast("ColumnElement[int | None]", chunk_counts.c.chunk_count)
    last_index_col = cast("ColumnElement[int | None]", ReadingPosition.chunk_index)
    return (
        select(
            UserLibraryEntry.book_id,
            GlobalBook.title,
            GlobalBook.author,
            UserLibraryEntry.category,
            UserLibraryEntry.added_at,
            chunk_count_col,
            last_index_col,
        )
        .join(GlobalBook, GlobalBook.book_id == UserLibraryEntry.book_id)
        .outerjoin(
            ReadingPosition,
            (ReadingPosition.user_id == UserLibraryEntry.user_id)
            & (ReadingPosition.book_id == UserLibraryEntry.book_id),
        )
        .outerjoin(chunk_counts, chunk_counts.c.book_id == UserLibraryEntry.book_id)
        .where(UserLibraryEntry.user_id == user_id)
        .order_by(UserLibraryEntry.added_at.desc())
    )


def _progress(last_chunk_index: int | None, chunk_count: int | None) -> float | None:
    """Chunks-completed ratio: ``(last_chunk_index + 1) / chunk_count``.

    ``None`` when there is no saved position or no countable chunks;
    clamped to 1.0 so a stale position past a re-ingested (shorter) book
    never reports >100%.
    """
    if last_chunk_index is None or not chunk_count:
        return None
    return min((last_chunk_index + 1) / chunk_count, 1.0)


@router.get("", response_model=LibraryResponse)
async def list_library(
    current_user: CurrentUserDep,
    session: SessionDep,
) -> LibraryResponse:
    """List the authenticated user's books, newest first, with progress."""
    result = await session.execute(_library_stmt(current_user.user_id))
    books = [
        LibraryBook(
            book_id=book_id,
            title=title,
            author=author,
            category=category,
            added_at=added_at,
            chunk_count=chunk_count,
            last_chunk_index=last_chunk_index,
            progress=_progress(last_chunk_index, chunk_count),
        )
        for (
            book_id,
            title,
            author,
            category,
            added_at,
            chunk_count,
            last_chunk_index,
        ) in result.tuples().all()
    ]
    return LibraryResponse(books=books)
