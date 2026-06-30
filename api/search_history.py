"""Search-history routes — the "Recent" panel's saved summary searches (Phase 51).

The read/delete surface over ``search_history`` (migration 0013), plus the
best-effort SAVE helper ``api/summary.py`` calls after a successful summary. A
``/search-summary`` run is the platform's most expensive request (the 4-leg
embed → rerank → highlight → LLM pipeline, 2–4 min wall time), so each
successful run is saved WHOLE — the user reopens a past search from the panel
and the saved ``result`` blob renders instantly, with no re-run and no re-pay:

- ``GET /search-history`` — the caller's saved searches, newest-first,
  LIGHTWEIGHT: ``query`` + scope + ``created_at`` + a short summary PREVIEW.
  The full ``result`` (citations carry the pruned chunk text — the big part of
  the blob) is NOT shipped here; the per-id GET serves it.
- ``GET /search-history/{history_id}`` — one saved search INCLUDING ``result``
  (the whole replayable ``SummaryResponse``) so the panel can rehydrate the
  summary + citation render. A non-owned, nonexistent, or non-UUID id is a
  uniform 404 with no existence oracle.
- ``DELETE /search-history/{history_id}`` — HARD delete; a non-owned id is the
  same 404.
- ``DELETE /search-history`` — clear the caller's whole history (204).

## Tenant gate (load-bearing)

``search_history`` is user-owned like ``documents`` / ``sermon_events``: EVERY
query filters by ``user_id`` derived from the JWT (``current_user.user_id``),
never from the body, query params, or path. The per-id endpoints resolve the
row through ``_require_owned_history`` FIRST — non-UUID garbage, nonexistent
ids, and another tenant's row all collapse to one byte-identical 404 (the
Phase 20 ``/tasks`` no-existence-oracle posture). Path ids are never
capabilities. These routes take NO JSON body, so there is no field to smuggle a
``user_id`` through; the smuggle vector lives only on the ``/search-summary``
body (``SummaryRequest``, ``extra="forbid"``) that feeds the save helper.

Every statement is factored into a module-level ``_xxx_stmt`` builder so the
``user_id`` scoping can be compile-pinned in ``tests/test_search_history_unit.py``
without a live database (the ``library._library_stmt`` pattern) — the mechanical
tenant audit.

## Save + retention (``record_search_history``)

``api/summary.py`` calls ``record_search_history`` after a successful summary.
It is BEST-EFFORT: the insert + the retention prune run inside one guarded
transaction, and ANY failure is caught + logged and swallowed — a history-write
failure must NEVER turn the costly, already-computed summary into a 5xx (the row
is a convenience, not part of the answer). ``SEARCH_HISTORY_RETENTION`` caps the
newest rows kept per user; ``_prune_stmt`` deletes everything older in the SAME
transaction as the insert (no existing per-user cap convention — this mirrors
the ``MATERIALIZER_CAP_ROWS`` explicit-named-constant style).
"""

from __future__ import annotations

import logging
import uuid
from contextlib import suppress
from datetime import datetime
from typing import Any

from db import SearchHistory
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import ReturningDelete

from auth import CurrentUserDep, SessionDep

router = APIRouter(prefix="/search-history", tags=["search-history"])

logger = logging.getLogger(__name__)

# Per-user retention cap: the newest N saved searches are kept, older rows are
# pruned in the SAME transaction as each insert (``record_search_history`` /
# ``_prune_stmt``). A *new* convention — the app had no per-user cap before —
# modeled on the ``MATERIALIZER_CAP_ROWS`` explicit-named-constant style. Read
# as a module global at save time so a test can monkeypatch it small.
SEARCH_HISTORY_RETENTION = 100

# The list endpoint ships only the first this-many chars of the summary as a
# preview (the ``documents.PREVIEW_CHARS`` precedent), never the full citations
# blob — the per-id GET serves the whole ``result``.
SUMMARY_PREVIEW_CHARS = 280


class SearchHistoryItem(BaseModel):
    """One row in the lightweight list — NO ``result`` / citations blob.

    ``summary_preview`` is the first ``SUMMARY_PREVIEW_CHARS`` chars of the saved
    summary; ``scope_book_ids`` / ``scope_collection_ids`` are the Phase 49 scope
    the search ran under.
    """

    history_id: uuid.UUID
    query: str
    scope_book_ids: list[uuid.UUID]
    scope_collection_ids: list[uuid.UUID]
    summary_preview: str
    created_at: datetime


class SearchHistoryEntry(BaseModel):
    """One full saved search INCLUDING ``result`` — the instant-replay shape.

    ``result`` is the serialized ``SummaryResponse`` (``summary`` + ``citations``
    + ``degraded``) exactly as it was returned, so the panel rehydrates the
    summary + citation render with no second ``/search-summary`` call.
    """

    history_id: uuid.UUID
    query: str
    scope_book_ids: list[uuid.UUID]
    scope_collection_ids: list[uuid.UUID]
    result: dict[str, Any]
    created_at: datetime


class SearchHistoryListResponse(BaseModel):
    items: list[SearchHistoryItem]


def _list_stmt(
    user_id: uuid.UUID,
) -> Select[tuple[uuid.UUID, str, list[str], list[str], datetime, str | None]]:
    """Build the lightweight newest-first list, scoped to the JWT user.

    Selects only the columns the panel needs and projects the summary text
    server-side via ``jsonb_extract_path_text`` so the heavy ``result``
    (citations carry full chunk text) never crosses the wire for the list. The
    ``user_id`` filter is the load-bearing tenant line: drop it and every user
    sees every user's searches. Ordered by ``created_at`` DESC (rides
    ``ix_search_history_user_created``); capped at ``SEARCH_HISTORY_RETENTION``
    (the table never holds more per user anyway). ``user_id`` is ALWAYS the JWT
    value.
    """
    return (
        select(
            SearchHistory.history_id,
            SearchHistory.query,
            SearchHistory.scope_book_ids,
            SearchHistory.scope_collection_ids,
            SearchHistory.created_at,
            func.jsonb_extract_path_text(SearchHistory.result, "summary").label("summary"),
        )
        .where(SearchHistory.user_id == user_id)
        .order_by(SearchHistory.created_at.desc())
        .limit(SEARCH_HISTORY_RETENTION)
    )


def _owned_history_stmt(
    history_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Select[tuple[SearchHistory]]:
    """Build the doubly-scoped single-row lookup (GET-full / DELETE gate).

    Both predicates are load-bearing: ``history_id`` from the path, ``user_id``
    ALWAYS from the JWT. Drop ``user_id`` and any authenticated user reads any
    saved search — a non-owned id is a 404 with no existence oracle (the Phase
    20 ``/tasks`` posture).
    """
    return select(SearchHistory).where(
        SearchHistory.history_id == history_id,
        SearchHistory.user_id == user_id,
    )


def _delete_stmt(
    history_id: uuid.UUID,
    user_id: uuid.UUID,
) -> ReturningDelete[tuple[uuid.UUID]]:
    """Build the HARD-delete DELETE for one owned row.

    Doubly-scoped by ``history_id`` AND ``user_id`` (the tenant gate).
    ``RETURNING history_id`` lets the handler tell "deleted one row" from
    "matched nothing -> 404" without a prior SELECT. ``user_id`` is ALWAYS the
    JWT value.
    """
    return (
        delete(SearchHistory)
        .where(
            SearchHistory.history_id == history_id,
            SearchHistory.user_id == user_id,
        )
        .returning(SearchHistory.history_id)
    )


def _clear_all_stmt(user_id: uuid.UUID) -> ReturningDelete[tuple[uuid.UUID]]:
    """Build the clear-all DELETE for the JWT user's whole history.

    Singly-scoped by ``user_id`` (the tenant gate) — it can only ever touch the
    caller's own rows. ``RETURNING history_id`` is informational (the handler
    always 204s). ``user_id`` is ALWAYS the JWT value.
    """
    return (
        delete(SearchHistory)
        .where(SearchHistory.user_id == user_id)
        .returning(SearchHistory.history_id)
    )


def _prune_stmt(user_id: uuid.UUID, *, keep: int) -> ReturningDelete[tuple[uuid.UUID]]:
    """Build the retention prune: delete the JWT user's rows beyond the newest *keep*.

    The ``overflow`` subquery lists the user's history ids ranked newest-first
    and SKIPS the newest ``keep`` (``OFFSET keep``); the outer DELETE removes
    exactly those older ids. BOTH the inner and outer queries carry the
    ``user_id`` predicate (the tenant gate, twice — defense in depth): a prune
    can only ever delete the caller's own rows. Runs in the SAME transaction as
    the insert (``record_search_history``). ``user_id`` is ALWAYS the JWT value.
    """
    overflow = (
        select(SearchHistory.history_id)
        .where(SearchHistory.user_id == user_id)
        .order_by(SearchHistory.created_at.desc())
        .offset(keep)
    )
    return (
        delete(SearchHistory)
        .where(
            SearchHistory.user_id == user_id,
            SearchHistory.history_id.in_(overflow),
        )
        .returning(SearchHistory.history_id)
    )


_HISTORY_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="Search history entry not found.",
)


def _to_uuids(values: list[str]) -> list[uuid.UUID]:
    """Parse a JSONB list of UUID strings back into ``uuid.UUID`` objects.

    The scope columns store text (the JSONB convention); a stored value is
    always one this service wrote from validated UUIDs, so this never sees
    garbage.
    """
    return [uuid.UUID(value) for value in values]


async def _require_owned_history(
    history_id: str,
    user_id: uuid.UUID,
    session: AsyncSession,
) -> SearchHistory:
    """Return the owned row or raise a no-oracle 404.

    Non-UUID garbage, nonexistent ids, and another tenant's row are
    byte-identical 404s — no existence oracle (the ``calendar._require_owned_event``
    contract). ``user_id`` is ALWAYS the JWT value.
    """
    try:
        parsed = uuid.UUID(history_id)
    except ValueError as exc:
        # Not a UUID → cannot be a search_history PK → same 404 shape.
        raise _HISTORY_NOT_FOUND from exc
    result = await session.execute(_owned_history_stmt(parsed, user_id))
    entry = result.scalar_one_or_none()
    if entry is None:
        raise _HISTORY_NOT_FOUND
    return entry


async def record_search_history(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    query: str,
    scope_book_ids: list[uuid.UUID] | None,
    scope_collection_ids: list[uuid.UUID] | None,
    result: dict[str, Any],
) -> None:
    """Best-effort: save one summary run + prune beyond the retention cap.

    Called by ``api/summary.py`` AFTER a successful summary. The insert and the
    retention prune commit in ONE transaction. ANY failure is caught, logged,
    and swallowed (the session rolled back) — a history-write failure must NEVER
    turn the costly, already-computed summary into a 5xx. The scope is stored as
    UUID strings (the JSONB text convention); ``result`` is the serialized
    ``SummaryResponse``. ``user_id`` is ALWAYS the JWT value.
    """
    try:
        session.add(
            SearchHistory(
                user_id=user_id,
                query=query,
                scope_book_ids=[str(book_id) for book_id in (scope_book_ids or [])],
                scope_collection_ids=[str(cid) for cid in (scope_collection_ids or [])],
                result=result,
            ),
        )
        # Flush so the new row participates in the prune's newest-first ranking.
        await session.flush()
        await session.execute(_prune_stmt(user_id, keep=SEARCH_HISTORY_RETENTION))
        await session.commit()
    except Exception:  # noqa: BLE001 — best-effort; a history write must never 5xx the summary
        logger.warning("failed to record search history", exc_info=True)
        # Roll back so the request's session is reusable; rollback may itself
        # fail on a duck-typed/dead session, so suppress that too.
        with suppress(Exception):
            await session.rollback()


@router.get("", response_model=SearchHistoryListResponse)
async def list_history(
    current_user: CurrentUserDep,
    session: SessionDep,
) -> SearchHistoryListResponse:
    """List the caller's saved searches, newest-first, with a summary preview.

    Lightweight — the full ``result`` / citations blob is NOT shipped here (the
    per-id GET serves it).
    """
    rows = (await session.execute(_list_stmt(current_user.user_id))).all()
    items = [
        SearchHistoryItem(
            history_id=history_id,
            query=query,
            scope_book_ids=_to_uuids(scope_book_ids),
            scope_collection_ids=_to_uuids(scope_collection_ids),
            summary_preview=(summary or "")[:SUMMARY_PREVIEW_CHARS],
            created_at=created_at,
        )
        for history_id, query, scope_book_ids, scope_collection_ids, created_at, summary in rows
    ]
    return SearchHistoryListResponse(items=items)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def clear_history(
    current_user: CurrentUserDep,
    session: SessionDep,
) -> None:
    """Clear the caller's whole search history. Always 204 (empty history is a no-op)."""
    await session.execute(_clear_all_stmt(current_user.user_id))
    await session.commit()


@router.get("/{history_id}", response_model=SearchHistoryEntry)
async def get_history(
    history_id: str,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> SearchHistoryEntry:
    """Return one saved search INCLUDING ``result``; 404 (no oracle) otherwise."""
    entry = await _require_owned_history(history_id, current_user.user_id, session)
    return SearchHistoryEntry(
        history_id=entry.history_id,
        query=entry.query,
        scope_book_ids=_to_uuids(entry.scope_book_ids),
        scope_collection_ids=_to_uuids(entry.scope_collection_ids),
        result=entry.result,
        created_at=entry.created_at,
    )


@router.delete("/{history_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_history(
    history_id: str,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> None:
    """Hard-delete one saved search. 404 (no oracle) if not owned.

    A non-UUID / nonexistent / cross-tenant id is the same 404 — no existence
    oracle.
    """
    try:
        parsed = uuid.UUID(history_id)
    except ValueError as exc:
        raise _HISTORY_NOT_FOUND from exc
    result = await session.execute(_delete_stmt(parsed, current_user.user_id))
    if result.scalar_one_or_none() is None:
        raise _HISTORY_NOT_FOUND
    await session.commit()
