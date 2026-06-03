"""User library listing route.

``GET /library`` returns the authenticated user's books — every
``user_library`` row for the JWT-derived ``user_id`` joined to its shared
``global_books`` row for display metadata (title, author), newest first.
The web ``/library`` page (Phase 15) renders these as a table.

Tenant invariant (repo-root ``CLAUDE.md``): the ``user_id`` is ALWAYS
``current_user.user_id`` (the JWT ``sub``); the listing is filtered by it
server-side. ``global_books`` is shared-by-design (ARCHITECTURE.md §3/§4) —
a title is not user-scoped — so the inner join adds no tenant surface; the
``where(user_id == ...)`` on ``user_library`` is the only gate, and there is
no client-supplied ``user_id`` / ``book_id`` anywhere on this path.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from db import GlobalBook, UserLibraryEntry
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import Select, select

from auth import CurrentUserDep, SessionDep

router = APIRouter(prefix="/library", tags=["library"])


class LibraryBook(BaseModel):
    book_id: uuid.UUID
    title: str
    author: str | None = None
    category: str | None = None
    added_at: datetime


class LibraryResponse(BaseModel):
    books: list[LibraryBook]


def _library_stmt(
    user_id: uuid.UUID,
) -> Select[tuple[uuid.UUID, str, str | None, str | None, datetime]]:
    """Build the tenant-scoped library query for *user_id*.

    Factored out so the tenant filter can be asserted in a unit test
    without a live database (``tests/test_library_unit.py``). The WHERE
    clause is the load-bearing line: drop it and every user sees every
    user's library.
    """
    return (
        select(
            UserLibraryEntry.book_id,
            GlobalBook.title,
            GlobalBook.author,
            UserLibraryEntry.category,
            UserLibraryEntry.added_at,
        )
        .join(GlobalBook, GlobalBook.book_id == UserLibraryEntry.book_id)
        .where(UserLibraryEntry.user_id == user_id)
        .order_by(UserLibraryEntry.added_at.desc())
    )


@router.get("", response_model=LibraryResponse)
async def list_library(
    current_user: CurrentUserDep,
    session: SessionDep,
) -> LibraryResponse:
    """List the authenticated user's books, newest first."""
    result = await session.execute(_library_stmt(current_user.user_id))
    books = [
        LibraryBook(
            book_id=book_id,
            title=title,
            author=author,
            category=category,
            added_at=added_at,
        )
        for book_id, title, author, category, added_at in result.tuples().all()
    ]
    return LibraryResponse(books=books)
