"""Preaching-calendar routes — user-owned dated sermon events (Phase 38, B3).

The server half of the B3 calendar. ``sermon_events`` is a user-owned table
(migration 0007); Phases 39-42 build the web side on this surface. CRUD over
dated entries plus a creation-time weekly materializer:

- ``GET /calendar/events?start=&end=`` — the caller's events in the
  half-open DATE range ``[start, end)``: an event dated exactly ``end`` is
  EXCLUDED. ``start`` and ``end`` are DATE (day granularity); ``start`` must
  be ``<= end`` and the span is capped at ``RANGE_CAP_DAYS`` (**400**) — a
  wider span is a 422 (a year view is one call). Ordered by ``event_date``.
- ``POST /calendar/events`` — create. Body ``{event_date, title, series?,
  document_id?, repeat_weekly_until?}`` (``extra="forbid"``). If
  ``document_id`` is non-null it MUST resolve to a document the JWT user owns
  (active OR soft-deleted — ownership is what matters), else a uniform 404
  identical whether the doc is another tenant's or nonexistent (NO existence
  oracle). If ``repeat_weekly_until`` is set, DISCRETE weekly rows are
  materialized from ``event_date`` through that date inclusive, capped at
  ``MATERIALIZER_CAP_ROWS`` (**53**) — more is a 422. Each materialized row
  is an INDEPENDENT ``sermon_events`` row (no parent linkage), so each
  PATCHes / DELETEs on its own.
- ``GET /calendar/events/{event_id}`` — the full event; a non-owned,
  nonexistent, or non-UUID id is a uniform 404 with no existence oracle.
- ``PATCH /calendar/events/{event_id}`` — partial update of ``event_date``,
  ``title``, ``series``, and/or ``document_id`` (``extra="forbid"``; at least
  one field must be present). ``document_id`` may be set non-null (the SAME
  ownership check) or to ``null`` (detach). ``updated_at`` is bumped
  EXPLICITLY (the column has ``server_default`` but no ``onupdate``).
- ``DELETE /calendar/events/{event_id}`` — HARD delete (events are cheap and
  regenerable, unlike the soft-deleted documents); a non-owned id is a
  uniform 404.

## Tenant gate (load-bearing)

``sermon_events`` is user-owned like ``documents`` / ``highlights``: EVERY
query filters by ``user_id`` derived from the JWT (``current_user.user_id``),
never from the body, query params, or path. The per-id endpoints resolve the
row through ``_require_owned_event`` FIRST — non-UUID garbage, nonexistent
ids, and another tenant's event all collapse to one byte-identical 404 (the
Phase 20 ``/tasks`` no-existence-oracle posture). Path/body ids are never
capabilities.

``document_id`` arrives as ATTACKER-CONTROLLED body input on POST/PATCH: the
FK alone does not scope tenancy, so a non-null ``document_id`` is
ownership-checked against the JWT user's ``documents`` before any write
(``_document_owned_stmt``). A miss is the SAME 404 used for a nonexistent
event — no detail revealing whether the doc exists for another user (no
title/existence oracle); otherwise user B could link user A's document and
the calendar would leak its existence.

Every statement is factored into a module-level ``_xxx_stmt`` builder so the
``user_id`` scoping can be compile-pinned in ``tests/test_calendar_unit.py``
without a live database (the ``library._library_stmt`` pattern) — the
mechanical tenant audit. Request models set ``extra="forbid"`` (Phase 18): a
smuggled ``user_id`` is a hard 422 naming the field.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Annotated

from db import Document, SermonEvent
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import ReturningDelete, ReturningUpdate

from auth import CurrentUserDep, SessionDep

router = APIRouter(prefix="/calendar", tags=["calendar"])

# GET range cap (Phase 38 pre-made decision 2): the half-open span
# ``end - start`` must be <= this many days, so a full year-view is one
# call but an unbounded scan is a 422. Recorded in api/AGENTS.md +
# the PHASES.md row (the B3 open question's resolution).
RANGE_CAP_DAYS = 400

# Weekly-materializer cap (Phase 38 pre-made decision 2): a
# ``repeat_weekly_until`` that would produce more than this many DISCRETE
# weekly rows (counting the anchor) is a 422. 53 covers a full year of
# weekly services with a margin (52 + 1).
MATERIALIZER_CAP_ROWS = 53


class CalendarEventCreate(BaseModel):
    """POST body. No ``user_id`` field — that is JWT-derived, never client-set.

    ``extra="forbid"`` (Phase 18 posture): a smuggled ``user_id`` is a hard
    422, never a silently-dropped key. ``document_id`` is OPTIONAL and
    nullable; when non-null it is ownership-checked against the JWT user's
    documents (the FK alone does not scope tenancy). ``repeat_weekly_until``,
    when set, materializes DISCRETE weekly rows from ``event_date`` through
    that date inclusive — it must be ``>= event_date`` and the row count is
    capped at ``MATERIALIZER_CAP_ROWS``.
    """

    model_config = ConfigDict(extra="forbid")

    event_date: date
    title: str = Field(min_length=1, max_length=512)
    series: str | None = Field(default=None, max_length=512)
    document_id: uuid.UUID | None = None
    repeat_weekly_until: date | None = None


class CalendarEventUpdate(BaseModel):
    """PATCH body — partial. ``extra="forbid"``; at least one field required.

    Every field is optional. ``document_id`` distinguishes three states via
    ``model_fields_set``: ABSENT (leave the link alone), present-and-null
    (DETACH), or present-and-non-null (re-link — the SAME ownership check as
    POST). ``repeat_weekly_until`` is intentionally NOT a PATCH field:
    materialized rows are independent, so re-materializing on edit is not the
    contract (pre-made decision 6).
    """

    model_config = ConfigDict(extra="forbid")

    event_date: date | None = None
    title: str | None = Field(default=None, min_length=1, max_length=512)
    series: str | None = Field(default=None, max_length=512)
    document_id: uuid.UUID | None = None


class CalendarEvent(BaseModel):
    """One event — the GET-full / POST / PATCH response shape."""

    event_id: uuid.UUID
    event_date: date
    title: str
    series: str | None
    document_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class CalendarEventListResponse(BaseModel):
    events: list[CalendarEvent]


def _range_stmt(
    user_id: uuid.UUID,
    *,
    start: date,
    end: date,
) -> Select[tuple[SermonEvent]]:
    """Build the tenant-scoped half-open range query ``[start, end)``.

    Factored out so BOTH the ``user_id`` filter AND the half-open bounds can
    be compile-pinned without a live database (the ``library._library_stmt``
    pattern). The ``user_id`` filter is the load-bearing tenant line: drop it
    and every user sees every user's calendar. The ``event_date < end`` (NOT
    ``<= end``) is load-bearing for the half-open contract — an event dated
    exactly ``end`` is EXCLUDED. Ordered by ``event_date`` (rides
    ``ix_sermon_events_user_date``). ``user_id`` is ALWAYS the JWT value.
    """
    return (
        select(SermonEvent)
        .where(
            SermonEvent.user_id == user_id,
            SermonEvent.event_date >= start,
            SermonEvent.event_date < end,
        )
        .order_by(SermonEvent.event_date.asc())
    )


def _owned_event_stmt(event_id: uuid.UUID, user_id: uuid.UUID) -> Select[tuple[SermonEvent]]:
    """Build the doubly-scoped single-event lookup (GET-full / PATCH gate).

    Both predicates are load-bearing: ``event_id`` from the path, ``user_id``
    ALWAYS from the JWT. Drop ``user_id`` and any authenticated user reads any
    event — this is the tenant gate, and a non-owned id is a 404 with no
    existence oracle (the Phase 20 ``/tasks`` posture).
    """
    return select(SermonEvent).where(
        SermonEvent.event_id == event_id,
        SermonEvent.user_id == user_id,
    )


def _document_owned_stmt(document_id: uuid.UUID, user_id: uuid.UUID) -> Select[tuple[uuid.UUID]]:
    """Build the cross-table ownership pre-flight for an attacker-supplied doc.

    ``document_id`` arrives in the request body, so the FK alone does NOT
    scope tenancy: a non-null ``document_id`` MUST resolve to a document the
    JWT user owns before any write. Both ACTIVE and soft-deleted docs are
    acceptable (ownership is what matters, pre-made decision 4) — NO
    ``deleted_at IS NULL`` predicate. On a miss the caller raises the SAME 404
    as a nonexistent event: identical whether the doc is another tenant's or
    nonexistent (no existence/title oracle). ``user_id`` is ALWAYS the JWT
    value. Mirrors ``reader._membership_stmt``.
    """
    return select(Document.document_id).where(
        Document.document_id == document_id,
        Document.user_id == user_id,
    )


def _update_stmt(
    event_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    values: dict[str, object],
) -> ReturningUpdate[tuple[uuid.UUID, date, str, str | None, uuid.UUID | None, datetime, datetime]]:
    """Build the PATCH UPDATE: apply *values* + bump ``updated_at`` on an owned row.

    Doubly-scoped by ``event_id`` AND ``user_id`` (the tenant gate).
    ``updated_at`` is bumped EXPLICITLY via ``func.now()`` in the value set
    (the column has ``server_default`` but no ``onupdate`` — the schema-wide
    convention; ``documents._update_stmt`` does the same) so the new value
    reads back. ``RETURNING`` the full row avoids a second round-trip.
    ``user_id`` is ALWAYS the JWT value.
    """
    return (
        update(SermonEvent)
        .where(
            SermonEvent.event_id == event_id,
            SermonEvent.user_id == user_id,
        )
        .values(**values, updated_at=func.now())
        .returning(
            SermonEvent.event_id,
            SermonEvent.event_date,
            SermonEvent.title,
            SermonEvent.series,
            SermonEvent.document_id,
            SermonEvent.created_at,
            SermonEvent.updated_at,
        )
    )


def _delete_stmt(event_id: uuid.UUID, user_id: uuid.UUID) -> ReturningDelete[tuple[uuid.UUID]]:
    """Build the HARD-delete DELETE for an owned event.

    Doubly-scoped by ``event_id`` AND ``user_id`` (the tenant gate). Events
    are cheap and regenerable, so this is a real row delete (NOT a soft
    delete like documents). ``RETURNING event_id`` lets the handler tell
    "deleted one row" from "matched nothing -> 404" without a prior SELECT.
    ``user_id`` is ALWAYS the JWT value.
    """
    return (
        delete(SermonEvent)
        .where(
            SermonEvent.event_id == event_id,
            SermonEvent.user_id == user_id,
        )
        .returning(SermonEvent.event_id)
    )


def _weekly_dates(start: date, until: date) -> list[date]:
    """Discrete weekly dates from *start* through *until* inclusive.

    Steps by 7 days from the anchor ``start`` while ``<= until``. ``start``
    itself is always included (the anchor occurrence). The caller validates
    ``until >= start`` and caps the count at ``MATERIALIZER_CAP_ROWS`` BEFORE
    materializing, so this helper does no bounding of its own; it is the pure
    date arithmetic, unit-tested directly.
    """
    occurrences: list[date] = []
    current = start
    while current <= until:
        occurrences.append(current)
        current += timedelta(weeks=1)
    return occurrences


def _to_response(event: SermonEvent) -> CalendarEvent:
    return CalendarEvent(
        event_id=event.event_id,
        event_date=event.event_date,
        title=event.title,
        series=event.series,
        document_id=event.document_id,
        created_at=event.created_at,
        updated_at=event.updated_at,
    )


_EVENT_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="Event not found.",
)


async def _require_owned_document(
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    session: AsyncSession,
) -> None:
    """404 (no oracle) unless *document_id* belongs to *user_id*.

    The cross-table ownership pre-flight for the attacker-controlled
    ``document_id`` body field. A miss raises the SAME 404 as a nonexistent
    event — identical whether the doc is another tenant's or nonexistent, so
    there is no existence/title oracle for another user's documents (the
    pre-made decision 4 posture). ``user_id`` is ALWAYS the JWT value.
    """
    owned = await session.execute(_document_owned_stmt(document_id, user_id))
    if owned.scalar_one_or_none() is None:
        raise _EVENT_NOT_FOUND


async def _require_owned_event(
    event_id: str,
    user_id: uuid.UUID,
    session: AsyncSession,
) -> SermonEvent:
    """Return the owned event or raise a no-oracle 404.

    Non-UUID garbage, nonexistent ids, and another tenant's event are
    byte-identical 404s — no existence oracle (the ``reader._require_owned_book``
    contract). ``user_id`` is ALWAYS the JWT value.
    """
    try:
        event_uuid = uuid.UUID(event_id)
    except ValueError as exc:
        # Not a UUID → cannot be a sermon_events PK → same 404 shape.
        raise _EVENT_NOT_FOUND from exc
    result = await session.execute(_owned_event_stmt(event_uuid, user_id))
    event = result.scalar_one_or_none()
    if event is None:
        raise _EVENT_NOT_FOUND
    return event


@router.get("/events", response_model=CalendarEventListResponse)
async def list_events(
    current_user: CurrentUserDep,
    session: SessionDep,
    start: Annotated[date, Query(description="Inclusive start of the range (DATE).")],
    end: Annotated[date, Query(description="Exclusive end of the range (DATE).")],
) -> CalendarEventListResponse:
    """List the caller's events in the half-open DATE range ``[start, end)``.

    ``start`` must be ``<= end`` and the span ``end - start`` must be ``<=
    RANGE_CAP_DAYS`` — otherwise a 422. An event dated exactly ``end`` is
    EXCLUDED (half-open). Ordered by ``event_date``.
    """
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Range start must be on or before end.",
        )
    if (end - start).days > RANGE_CAP_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Range span exceeds the {RANGE_CAP_DAYS}-day cap.",
        )
    result = await session.execute(_range_stmt(current_user.user_id, start=start, end=end))
    events = [_to_response(event) for event in result.scalars().all()]
    return CalendarEventListResponse(events=events)


@router.post(
    "/events",
    response_model=CalendarEventListResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_events(
    payload: CalendarEventCreate,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> CalendarEventListResponse:
    """Create one event, or a capped run of weekly events via the materializer.

    If ``document_id`` is non-null it is ownership-checked FIRST (404 no
    oracle on a miss). If ``repeat_weekly_until`` is set, DISCRETE weekly rows
    are materialized from ``event_date`` through it inclusive — it must be
    ``>= event_date`` (else 422) and the row count is capped at
    ``MATERIALIZER_CAP_ROWS`` (else 422). Each row is INDEPENDENT. Returns
    every created event in ``event_date`` order.
    """
    # Ownership gate FIRST: a non-null document_id must belong to the JWT
    # user (no write happens on a miss). Identical 404 whether the doc is
    # another tenant's or nonexistent (no existence oracle).
    if payload.document_id is not None:
        await _require_owned_document(payload.document_id, current_user.user_id, session)

    if payload.repeat_weekly_until is not None:
        if payload.repeat_weekly_until < payload.event_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="repeat_weekly_until must be on or after event_date.",
            )
        # Bound the work BEFORE generating. The occurrence count is pure O(1)
        # date arithmetic: weekly steps from the anchor that land on or before
        # the end, plus the anchor itself. Both operands are pydantic-validated
        # ``date`` values, so subtracting them CANNOT overflow (no date.max
        # arithmetic), and the cap is enforced before any list is built —
        # closing the far-future ``repeat_weekly_until`` memory/CPU DoS.
        occurrence_count = (payload.repeat_weekly_until - payload.event_date).days // 7 + 1
        if occurrence_count > MATERIALIZER_CAP_ROWS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Weekly recurrence exceeds the {MATERIALIZER_CAP_ROWS}-row cap.",
            )
        # Count is confirmed <= MATERIALIZER_CAP_ROWS, so the last generated
        # date is the anchor + (count-1) weekly steps, which is <=
        # repeat_weekly_until (itself a valid ``date``); the ``+= 7 days`` loop
        # therefore never steps past date.max. The try/except is
        # belt-and-suspenders: any residual overflow collapses to the SAME 422,
        # never an uncaught 500.
        try:
            occurrences = _weekly_dates(payload.event_date, payload.repeat_weekly_until)
        except OverflowError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Weekly recurrence exceeds the {MATERIALIZER_CAP_ROWS}-row cap.",
            ) from exc
    else:
        occurrences = [payload.event_date]

    events = [
        SermonEvent(
            user_id=current_user.user_id,
            event_date=occurrence,
            title=payload.title,
            series=payload.series,
            document_id=payload.document_id,
        )
        for occurrence in occurrences
    ]
    session.add_all(events)
    await session.commit()
    for event in events:
        await session.refresh(event)
    return CalendarEventListResponse(events=[_to_response(event) for event in events])


@router.get("/events/{event_id}", response_model=CalendarEvent)
async def get_event(
    event_id: str,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> CalendarEvent:
    """Return the full event for the JWT user; 404 (no oracle) otherwise."""
    event = await _require_owned_event(event_id, current_user.user_id, session)
    return _to_response(event)


@router.patch("/events/{event_id}", response_model=CalendarEvent)
async def update_event(
    event_id: str,
    payload: CalendarEventUpdate,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> CalendarEvent:
    """Partial update of an owned event. 404 (no oracle) if not owned.

    At least one of ``event_date`` / ``title`` / ``series`` / ``document_id``
    must be present (an empty patch is a 422). ``document_id`` is three-state
    via ``model_fields_set``: absent leaves the link alone, ``null`` detaches,
    and a non-null value is re-linked under the SAME ownership check (404 no
    oracle on a miss). ``updated_at`` is bumped EXPLICITLY (no ``onupdate``).
    """
    fields_set = payload.model_fields_set
    if not fields_set:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="PATCH must set at least one of event_date, title, series, document_id.",
        )

    # Re-linking to a non-null document_id needs the SAME ownership check as
    # POST — gate it (404 no oracle) BEFORE the ownership gate / write below.
    document_supplied = "document_id" in fields_set
    if document_supplied and payload.document_id is not None:
        await _require_owned_document(payload.document_id, current_user.user_id, session)

    # Ownership gate (404 no oracle); resolves the event FIRST so a
    # cross-tenant / nonexistent id never reaches the write.
    event = await _require_owned_event(event_id, current_user.user_id, session)

    values: dict[str, object] = {}
    if "event_date" in fields_set:
        values["event_date"] = payload.event_date
    if "title" in fields_set:
        values["title"] = payload.title
    if "series" in fields_set:
        values["series"] = payload.series
    if document_supplied:
        # Present-and-null = detach; present-and-non-null = re-link.
        values["document_id"] = payload.document_id

    row = (
        await session.execute(
            _update_stmt(event.event_id, current_user.user_id, values=values),
        )
    ).one()
    await session.commit()
    event_id_val, event_date, title, series, document_id, created_at, updated_at = row
    return CalendarEvent(
        event_id=event_id_val,
        event_date=event_date,
        title=title,
        series=series,
        document_id=document_id,
        created_at=created_at,
        updated_at=updated_at,
    )


@router.delete("/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(
    event_id: str,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> None:
    """Hard-delete the event. 404 (no oracle) if not owned.

    Events are cheap and regenerable (unlike soft-deleted documents), so this
    is a real row delete. A non-UUID / nonexistent / cross-tenant id is the
    same 404 — no existence oracle.
    """
    try:
        event_uuid = uuid.UUID(event_id)
    except ValueError as exc:
        raise _EVENT_NOT_FOUND from exc
    result = await session.execute(_delete_stmt(event_uuid, current_user.user_id))
    if result.scalar_one_or_none() is None:
        raise _EVENT_NOT_FOUND
    await session.commit()
