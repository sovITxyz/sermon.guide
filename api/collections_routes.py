"""Library-collection routes — user-owned folders that group books (Phase 48).

Wires up the dormant ``collections`` table (migration 0001) and its new
``collection_books`` membership table (migration 0011) so a user can organize
the books in their library into named folders. CRUD over collections plus
add/remove book membership:

- ``POST /collections`` — create. Body ``{name, description?}``
  (``extra="forbid"``). Returns the created collection (empty ``book_ids``).
- ``GET /collections`` — the caller's collections, newest-first, each with its
  current ``book_ids`` membership.
- ``GET /collections/{collection_id}`` — one collection with its ``book_ids``; a
  non-owned, nonexistent, or non-UUID id is a uniform 404 with no existence
  oracle.
- ``PATCH /collections/{collection_id}`` — partial update of ``name`` and/or
  ``description`` (``extra="forbid"``; at least one field must be present).
- ``DELETE /collections/{collection_id}`` — HARD delete; its
  ``collection_books`` rows cascade away via the FK.
- ``POST /collections/{collection_id}/books`` — add books. Body
  ``{book_ids}``. The requested set is CLAMPED to the owner's ``user_library``
  (a book the user does not own is silently dropped) and inserted
  ``ON CONFLICT (collection_id, book_id) DO NOTHING`` (idempotent re-add).
- ``DELETE /collections/{collection_id}/books`` — remove books. Body
  ``{book_ids}``. Removing a book not in the collection is a no-op.

## Tenant gate (load-bearing)

``collections`` and ``collection_books`` are user-owned like ``documents`` /
``sermon_events``: EVERY query filters by ``user_id`` derived from the JWT
(``current_user.user_id``), never from the body, query params, or path. The
per-id endpoints resolve the row through ``_require_owned_collection`` FIRST —
non-UUID garbage, nonexistent ids, and another tenant's collection all collapse
to one byte-identical 404 (the Phase 20 ``/tasks`` no-existence-oracle posture).
Path/body ids are never capabilities.

``book_ids`` arrives as ATTACKER-CONTROLLED body input on the add-books path:
the FK alone does not scope tenancy, so the requested set is INTERSECTED with
the owner's ``user_library`` (``_library_subset_stmt``) before any insert — a
membership can never name a book the JWT user does not own, and a smuggled
foreign ``book_id`` is silently clamped out (not an oracle-leaking error).
``collection_books.user_id`` is the DENORMALIZED JWT user on every insert.

Every statement is factored into a module-level ``_xxx_stmt`` builder so the
``user_id`` scoping can be compile-pinned in ``tests/test_collections_unit.py``
without a live database (the ``library._library_stmt`` pattern) — the
mechanical tenant audit. Request models set ``extra="forbid"`` (Phase 18): a
smuggled ``user_id`` is a hard 422 naming the field.

``_owned_collection_ids_stmt`` and ``_member_book_ids_stmt`` are EXPORTED for
Phase 49 (scoped search): the search path ownership-checks client-supplied
``collection_ids`` and resolves them to member ``book_ids`` through these same
builders, so the collection tenant boundary is enforced once. The import is
one-directional (search imports collections), no cycle.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from db import Collection, CollectionBook, UserLibraryEntry
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.postgresql.dml import Insert as PgInsert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import ReturningDelete, ReturningUpdate

from auth import CurrentUserDep, SessionDep

router = APIRouter(prefix="/collections", tags=["collections"])

# Length caps live here — the API is the single 422 owner (the web whitelists do
# structural-only checks). ``name`` matches the ``collections.name`` String(255)
# column; ``description`` is a Text column capped to a sane size; the add/remove
# book-set cap mirrors the Phase 49 ``book_ids`` cap (a 10K-book library can be
# bulk-assigned in one call).
NAME_MAX_LENGTH = 255
DESCRIPTION_MAX_LENGTH = 2000
BOOK_IDS_CAP = 10_000


class CollectionCreate(BaseModel):
    """POST body. No ``user_id`` field — that is JWT-derived, never client-set.

    ``extra="forbid"`` (Phase 18 posture): a smuggled ``user_id`` is a hard
    422, never a silently-dropped key. ``description`` is OPTIONAL and nullable.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)


class CollectionUpdate(BaseModel):
    """PATCH body — partial. ``extra="forbid"``; at least one field required.

    ``name`` distinguishes ABSENT (leave it) from present (rename) via
    ``model_fields_set``; a present-and-``null`` ``name`` is a 422 (the column
    is NOT NULL). ``description`` is three-state: ABSENT (leave it),
    present-and-``null`` (clear it), present-and-non-null (replace it).
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX_LENGTH)
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)


class CollectionBooksRequest(BaseModel):
    """Add-/remove-books body. ``extra="forbid"``; ``book_ids`` is required.

    The cap is the single 422 owner (the web whitelist does structural-only
    checks). On the add path the set is CLAMPED to the owner's ``user_library``
    server-side — a smuggled foreign ``book_id`` is silently dropped, never an
    error that would oracle another tenant's book.
    """

    model_config = ConfigDict(extra="forbid")

    book_ids: list[uuid.UUID] = Field(min_length=1, max_length=BOOK_IDS_CAP)


class CollectionResponse(BaseModel):
    """One collection — the GET / POST / PATCH / books response shape.

    ``book_ids`` is the collection's CURRENT membership (the JWT user's rows),
    in insertion order.
    """

    collection_id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    book_ids: list[uuid.UUID]


class CollectionListResponse(BaseModel):
    collections: list[CollectionResponse]


def _list_stmt(user_id: uuid.UUID) -> Select[tuple[Collection]]:
    """Build the tenant-scoped collection list, newest-first.

    The ``user_id`` filter is the load-bearing tenant line: drop it and every
    user sees every user's collections. Ordered by ``created_at`` DESC (rides
    ``ix_collections_user_id``'s user prefix). ``user_id`` is ALWAYS the JWT
    value.
    """
    return (
        select(Collection)
        .where(Collection.user_id == user_id)
        .order_by(Collection.created_at.desc())
    )


def _owned_collection_stmt(
    collection_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Select[tuple[Collection]]:
    """Build the doubly-scoped single-collection lookup (the tenant gate).

    Both predicates are load-bearing: ``collection_id`` from the path,
    ``user_id`` ALWAYS from the JWT. Drop ``user_id`` and any authenticated
    user reads any collection — a non-owned id is a 404 with no existence
    oracle (the Phase 20 ``/tasks`` posture).
    """
    return select(Collection).where(
        Collection.collection_id == collection_id,
        Collection.user_id == user_id,
    )


def _owned_collection_ids_stmt(
    collection_ids: Sequence[uuid.UUID],
    user_id: uuid.UUID,
) -> Select[tuple[uuid.UUID]]:
    """Build the bulk ownership clamp: which of *collection_ids* the user owns.

    EXPORTED for Phase 49 (scoped search): the search path ownership-checks
    client-supplied ``collection_ids`` here before resolving them to member
    books, so a foreign/nonexistent collection contributes nothing (the
    no-oracle posture, in set form). The ``user_id`` filter is load-bearing —
    ALWAYS the JWT value. An empty ``collection_ids`` yields a false predicate
    (no rows), which is correct.
    """
    return select(Collection.collection_id).where(
        Collection.collection_id.in_(collection_ids),
        Collection.user_id == user_id,
    )


def _member_book_ids_stmt(
    collection_ids: Sequence[uuid.UUID],
    user_id: uuid.UUID,
) -> Select[tuple[uuid.UUID]]:
    """Build the member-book resolver for *collection_ids*, scoped to the user.

    EXPORTED for Phase 49 (scoped search): resolves owned collection ids to the
    ``book_id`` set they contain. The ``user_id`` filter rides the denormalized
    tenant column so this never joins back to ``collections`` — ALWAYS the JWT
    value. Callers MUST pass collection ids already ownership-checked via
    ``_owned_collection_ids_stmt`` (defense in depth: the ``user_id`` predicate
    here is the second gate). An empty ``collection_ids`` yields no rows.
    """
    return select(CollectionBook.book_id).where(
        CollectionBook.collection_id.in_(collection_ids),
        CollectionBook.user_id == user_id,
    )


def _memberships_stmt(user_id: uuid.UUID) -> Select[tuple[uuid.UUID, uuid.UUID]]:
    """Build the all-memberships scan for the list endpoint, scoped to the user.

    Returns ``(collection_id, book_id)`` for every membership the JWT user owns,
    so the list handler can group ``book_ids`` per collection in one query
    (no N+1). The ``user_id`` filter is the load-bearing tenant line — ALWAYS
    the JWT value.
    """
    return select(CollectionBook.collection_id, CollectionBook.book_id).where(
        CollectionBook.user_id == user_id,
    )


def _library_subset_stmt(
    book_ids: Sequence[uuid.UUID],
    user_id: uuid.UUID,
) -> Select[tuple[uuid.UUID]]:
    """Build the add-books library clamp: which of *book_ids* the user owns.

    ``book_ids`` is attacker-controlled body input, so the requested set is
    INTERSECTED with the owner's ``user_library`` before any membership insert
    — a membership can never name a book the JWT user does not own. The
    ``user_id`` filter is load-bearing — ALWAYS the JWT value. Mirrors the
    server-side library resolution every search uses (CLAUDE.md). An empty
    ``book_ids`` yields no rows.
    """
    return select(UserLibraryEntry.book_id).where(
        UserLibraryEntry.book_id.in_(book_ids),
        UserLibraryEntry.user_id == user_id,
    )


def _add_books_stmt(
    collection_id: uuid.UUID,
    user_id: uuid.UUID,
    book_ids: Sequence[uuid.UUID],
) -> PgInsert:
    """Build the membership INSERT … ON CONFLICT (collection, book) DO NOTHING.

    One row per *book_id*, each carrying the DENORMALIZED JWT ``user_id``. The
    conflict target is ``uq_collection_books_collection_book`` — re-adding a
    book already in the collection is an idempotent no-op. The caller passes
    only library-clamped ids (``_library_subset_stmt``), so every inserted
    membership is owned. ``user_id`` is ALWAYS the JWT value; the caller
    guarantees *book_ids* is non-empty.
    """
    rows = [
        {
            "collection_book_id": uuid.uuid4(),
            "collection_id": collection_id,
            "user_id": user_id,
            "book_id": book_id,
        }
        for book_id in book_ids
    ]
    stmt = pg_insert(CollectionBook).values(rows)
    return stmt.on_conflict_do_nothing(constraint="uq_collection_books_collection_book")


def _remove_books_stmt(
    collection_id: uuid.UUID,
    user_id: uuid.UUID,
    book_ids: Sequence[uuid.UUID],
) -> ReturningDelete[tuple[uuid.UUID]]:
    """Build the membership DELETE, triply-scoped (collection, user, book set).

    Both the ``collection_id`` and the ``user_id`` predicates are load-bearing
    (the denormalized tenant gate); ``book_id IN (…)`` bounds the removal to the
    requested set. Removing a book not in the collection matches zero rows (a
    no-op). ``RETURNING book_id`` lets the handler report what was removed.
    ``user_id`` is ALWAYS the JWT value.
    """
    return (
        delete(CollectionBook)
        .where(
            CollectionBook.collection_id == collection_id,
            CollectionBook.user_id == user_id,
            CollectionBook.book_id.in_(book_ids),
        )
        .returning(CollectionBook.book_id)
    )


def _update_stmt(
    collection_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    values: dict[str, object],
) -> ReturningUpdate[tuple[uuid.UUID, str, str | None, datetime]]:
    """Build the PATCH UPDATE: apply *values* on an owned collection.

    Doubly-scoped by ``collection_id`` AND ``user_id`` (the tenant gate).
    ``collections`` has no ``updated_at`` column, so there is nothing to bump
    (unlike ``documents`` / ``sermon_events``). ``RETURNING`` the row avoids a
    second round-trip. ``user_id`` is ALWAYS the JWT value.
    """
    return (
        update(Collection)
        .where(
            Collection.collection_id == collection_id,
            Collection.user_id == user_id,
        )
        .values(**values)
        .returning(
            Collection.collection_id,
            Collection.name,
            Collection.description,
            Collection.created_at,
        )
    )


def _delete_stmt(
    collection_id: uuid.UUID,
    user_id: uuid.UUID,
) -> ReturningDelete[tuple[uuid.UUID]]:
    """Build the HARD-delete DELETE for an owned collection.

    Doubly-scoped by ``collection_id`` AND ``user_id`` (the tenant gate). The
    collection's ``collection_books`` rows cascade away via the FK. ``RETURNING
    collection_id`` lets the handler tell "deleted one row" from "matched
    nothing -> 404" without a prior SELECT. ``user_id`` is ALWAYS the JWT value.
    """
    return (
        delete(Collection)
        .where(
            Collection.collection_id == collection_id,
            Collection.user_id == user_id,
        )
        .returning(Collection.collection_id)
    )


_COLLECTION_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="Collection not found.",
)


def _to_response(collection: Collection, *, book_ids: Sequence[uuid.UUID]) -> CollectionResponse:
    return CollectionResponse(
        collection_id=collection.collection_id,
        name=collection.name,
        description=collection.description,
        created_at=collection.created_at,
        book_ids=list(book_ids),
    )


async def _require_owned_collection(
    collection_id: str,
    user_id: uuid.UUID,
    session: AsyncSession,
) -> Collection:
    """Return the owned collection or raise a no-oracle 404.

    Non-UUID garbage, nonexistent ids, and another tenant's collection are
    byte-identical 404s — no existence oracle (the ``calendar._require_owned_event``
    contract). ``user_id`` is ALWAYS the JWT value.
    """
    try:
        collection_uuid = uuid.UUID(collection_id)
    except ValueError as exc:
        # Not a UUID → cannot be a collections PK → same 404 shape.
        raise _COLLECTION_NOT_FOUND from exc
    result = await session.execute(_owned_collection_stmt(collection_uuid, user_id))
    collection = result.scalar_one_or_none()
    if collection is None:
        raise _COLLECTION_NOT_FOUND
    return collection


async def _collection_book_ids(
    collection_id: uuid.UUID,
    user_id: uuid.UUID,
    session: AsyncSession,
) -> list[uuid.UUID]:
    """Return the JWT user's member ``book_ids`` for one owned collection."""
    result = await session.execute(_member_book_ids_stmt([collection_id], user_id))
    return list(result.scalars().all())


@router.post("", response_model=CollectionResponse, status_code=status.HTTP_201_CREATED)
async def create_collection(
    payload: CollectionCreate,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> CollectionResponse:
    """Create a collection owned by the JWT user (empty membership)."""
    collection = Collection(
        user_id=current_user.user_id,
        name=payload.name,
        description=payload.description,
    )
    session.add(collection)
    await session.commit()
    await session.refresh(collection)
    return _to_response(collection, book_ids=[])


@router.get("", response_model=CollectionListResponse)
async def list_collections(
    current_user: CurrentUserDep,
    session: SessionDep,
) -> CollectionListResponse:
    """List the caller's collections, newest-first, each with its ``book_ids``."""
    result = await session.execute(_list_stmt(current_user.user_id))
    collections = list(result.scalars().all())
    memberships = await session.execute(_memberships_stmt(current_user.user_id))
    by_collection: dict[uuid.UUID, list[uuid.UUID]] = {}
    for collection_id, book_id in memberships.tuples().all():
        by_collection.setdefault(collection_id, []).append(book_id)
    return CollectionListResponse(
        collections=[
            _to_response(c, book_ids=by_collection.get(c.collection_id, [])) for c in collections
        ],
    )


@router.get("/{collection_id}", response_model=CollectionResponse)
async def get_collection(
    collection_id: str,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> CollectionResponse:
    """Return one collection with its ``book_ids``; 404 (no oracle) otherwise."""
    collection = await _require_owned_collection(collection_id, current_user.user_id, session)
    book_ids = await _collection_book_ids(collection.collection_id, current_user.user_id, session)
    return _to_response(collection, book_ids=book_ids)


@router.patch("/{collection_id}", response_model=CollectionResponse)
async def update_collection(
    collection_id: str,
    payload: CollectionUpdate,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> CollectionResponse:
    """Partial update of an owned collection. 404 (no oracle) if not owned.

    At least one of ``name`` / ``description`` must be present (an empty patch
    is a 422). A present-and-``null`` ``name`` is a 422 (the column is NOT
    NULL); ``description`` may be set to ``null`` to clear it.
    """
    fields_set = payload.model_fields_set
    if not fields_set:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="PATCH must set at least one of name, description.",
        )
    if "name" in fields_set and payload.name is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="name must not be null.",
        )

    # Ownership gate (404 no oracle); resolves the collection FIRST so a
    # cross-tenant / nonexistent id never reaches the write.
    collection = await _require_owned_collection(collection_id, current_user.user_id, session)

    values: dict[str, object] = {}
    if "name" in fields_set:
        values["name"] = payload.name
    if "description" in fields_set:
        values["description"] = payload.description

    row = (
        await session.execute(
            _update_stmt(collection.collection_id, current_user.user_id, values=values),
        )
    ).one()
    await session.commit()
    collection_id_val, name, description, created_at = row
    book_ids = await _collection_book_ids(collection_id_val, current_user.user_id, session)
    return CollectionResponse(
        collection_id=collection_id_val,
        name=name,
        description=description,
        created_at=created_at,
        book_ids=book_ids,
    )


@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_collection(
    collection_id: str,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> None:
    """Hard-delete the collection (memberships cascade). 404 (no oracle) if not owned.

    A non-UUID / nonexistent / cross-tenant id is the same 404 — no existence
    oracle.
    """
    try:
        collection_uuid = uuid.UUID(collection_id)
    except ValueError as exc:
        raise _COLLECTION_NOT_FOUND from exc
    result = await session.execute(_delete_stmt(collection_uuid, current_user.user_id))
    if result.scalar_one_or_none() is None:
        raise _COLLECTION_NOT_FOUND
    await session.commit()


@router.post("/{collection_id}/books", response_model=CollectionResponse)
async def add_books(
    collection_id: str,
    payload: CollectionBooksRequest,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> CollectionResponse:
    """Add books to an owned collection, clamped to the owner's library.

    The requested ``book_ids`` are INTERSECTED with the JWT user's
    ``user_library`` (a foreign/unowned book is silently dropped), deduped, and
    inserted ``ON CONFLICT (collection_id, book_id) DO NOTHING`` (idempotent
    re-add). Returns the collection with its refreshed ``book_ids``.
    """
    collection = await _require_owned_collection(collection_id, current_user.user_id, session)

    owned = await session.execute(
        _library_subset_stmt(payload.book_ids, current_user.user_id),
    )
    # Dedupe while preserving order (the request may repeat an id, and the
    # library subset is unordered relative to the request).
    clamped = list(dict.fromkeys(owned.scalars().all()))
    if clamped:
        await session.execute(
            _add_books_stmt(collection.collection_id, current_user.user_id, clamped),
        )
        await session.commit()

    book_ids = await _collection_book_ids(collection.collection_id, current_user.user_id, session)
    return _to_response(collection, book_ids=book_ids)


@router.delete("/{collection_id}/books", response_model=CollectionResponse)
async def remove_books(
    collection_id: str,
    payload: CollectionBooksRequest,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> CollectionResponse:
    """Remove books from an owned collection. Removing a non-member is a no-op.

    The DELETE is triply-scoped (collection, JWT user, requested book set), so a
    cross-tenant or non-member ``book_id`` simply matches nothing. Returns the
    collection with its refreshed ``book_ids``.
    """
    collection = await _require_owned_collection(collection_id, current_user.user_id, session)
    await session.execute(
        _remove_books_stmt(collection.collection_id, current_user.user_id, payload.book_ids),
    )
    await session.commit()
    book_ids = await _collection_book_ids(collection.collection_id, current_user.user_id, session)
    return _to_response(collection, book_ids=book_ids)
