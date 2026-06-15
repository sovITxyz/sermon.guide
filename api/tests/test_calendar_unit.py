"""Unit tests for the calendar (sermon_events) routes (Phase 38, B3 contract).

Pure-unit, no live infra — the ``test_documents_unit.py`` template. Route
tests boot ``main.app`` through ``with TestClient(app):`` (lifespan runs; dev
posture monkeypatched as settings ATTRIBUTES) and replace auth + the DB
session via ``app.dependency_overrides``. The fake session resolves
statements the way the DB would — routing on the compiled SQL, predicates
from the compiled params — and keys an event store on ``event_id`` plus a
document store on ``document_id`` (for the cross-table ownership pre-flight).

What this file pins:

- the statement builders (the mechanical tenant audit, no DB): ``_range_stmt``
  carries ``user_id`` AND the half-open ``event_date >= start`` / ``< end``;
  ``_owned_event_stmt`` / ``_update_stmt`` / ``_delete_stmt`` are doubly-scoped
  (``event_id`` AND ``user_id``); ``_document_owned_stmt`` carries ``user_id``
  AND has NO ``deleted_at`` predicate (ownership, active or soft-deleted);
- ``_weekly_dates`` directly (the pure materializer arithmetic);
- the half-open range: an event dated exactly ``end`` is EXCLUDED, an event on
  ``start`` is included; only the JWT user's events appear, in ``event_date``
  order; ``start > end`` and a span ``> 400`` days are each a 422;
- the weekly materializer: ``repeat_weekly_until`` produces the right discrete
  count; exactly 53 rows is accepted, 54 is a 422; ``until < event_date`` is a
  422; each materialized row is an INDEPENDENT row that PATCHes / DELETEs on
  its own;
- the cross-table ``document_id`` ownership check: a non-null body
  ``document_id`` that is another tenant's OR nonexistent is the SAME 404 on
  POST and PATCH (no existence/title oracle) and NOTHING is written; a
  JWT-owned document_id (active or soft-deleted) succeeds; PATCH with
  ``document_id: null`` DETACHES;
- the request-model posture: a smuggled ``user_id`` is a hard 422; an empty
  PATCH is a 422;
- the no-existence-oracle 404 matrix: cross-tenant, nonexistent, and non-UUID
  garbage event ids are byte-identical 404s on GET / PATCH / DELETE, write-free.
"""

# Tests exercise module-internals and pass duck-typed fakes on purpose.
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportArgumentType=false

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

import auth
import main as main_module
from calendar_routes import (
    MATERIALIZER_CAP_ROWS,
    RANGE_CAP_DAYS,
    _delete_stmt,
    _document_owned_stmt,
    _owned_event_stmt,
    _range_stmt,
    _update_stmt,
    _weekly_dates,
)
from settings import DEV_JWT_SECRET

# --- fakes -------------------------------------------------------------------


class _FakeUser:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()


class _StoredEvent:
    """A row in the fake sermon_events table — mutated in place by the routes."""

    def __init__(
        self,
        *,
        event_id: uuid.UUID,
        user_id: uuid.UUID,
        event_date: date,
        title: str,
        series: str | None,
        document_id: uuid.UUID | None,
        created_at: datetime,
        updated_at: datetime,
    ) -> None:
        self.event_id = event_id
        self.user_id = user_id
        self.event_date = event_date
        self.title = title
        self.series = series
        self.document_id = document_id
        self.created_at = created_at
        self.updated_at = updated_at


class _StoredDoc:
    """A row in the fake documents table — only the ownership-check fields."""

    def __init__(
        self,
        *,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
        deleted_at: datetime | None = None,
    ) -> None:
        self.document_id = document_id
        self.user_id = user_id
        self.deleted_at = deleted_at


class _ScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalars(self) -> _ScalarResult:
        return _ScalarResult(self._rows)

    def one(self) -> Any:
        assert len(self._rows) == 1
        return self._rows[0]


_UPDATE_TUPLE_COLS = (
    "event_id",
    "event_date",
    "title",
    "series",
    "document_id",
    "created_at",
    "updated_at",
)


class _FakeSession:
    """Duck-typed AsyncSession resolving statements the way the DB would.

    Statements route on their compiled SQL and resolve from the compiled
    params (the ``test_documents_unit.py`` philosophy). ``events`` is keyed by
    ``event_id``; ``docs`` backs the cross-table ownership pre-flight. ``add``
    / ``add_all`` stage ORM rows the route built; ``commit`` / ``refresh``
    mimic the insert + server-default read-back. ``executed`` records
    statement kinds in order so the gate-before-write ordering can be pinned
    and the 404 path proven write-free.
    """

    def __init__(
        self,
        *,
        events: dict[uuid.UUID, _StoredEvent] | None = None,
        docs: dict[uuid.UUID, _StoredDoc] | None = None,
    ) -> None:
        self.events: dict[uuid.UUID, _StoredEvent] = events or {}
        self.docs: dict[uuid.UUID, _StoredDoc] = docs or {}
        self.added: list[Any] = []
        self.executed: list[str] = []
        self.commits = 0
        self._clock = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)

    def _now(self) -> datetime:
        self._clock += timedelta(seconds=1)
        return self._clock

    def add(self, obj: Any) -> None:
        self.executed.append("add")
        self.added.append(obj)

    def add_all(self, objs: Any) -> None:
        for obj in objs:
            self.add(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: Any) -> None:
        if getattr(obj, "event_id", None) is None:
            obj.event_id = uuid.uuid4()
        now = self._now()
        if getattr(obj, "created_at", None) is None:
            obj.created_at = now
        if getattr(obj, "updated_at", None) is None:
            obj.updated_at = now
        self.events[obj.event_id] = _StoredEvent(
            event_id=obj.event_id,
            user_id=obj.user_id,
            event_date=obj.event_date,
            title=obj.title,
            series=obj.series,
            document_id=obj.document_id,
            created_at=obj.created_at,
            updated_at=obj.updated_at,
        )

    async def execute(self, stmt: Any) -> _FakeResult:
        compiled = stmt.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        params = compiled.params

        if sql.startswith("DELETE FROM sermon_events"):
            self.executed.append("delete")
            event = self.events.get(params["event_id_1"])
            if event is None or event.user_id != params["user_id_1"]:
                return _FakeResult([])
            del self.events[event.event_id]
            return _FakeResult([event.event_id])

        if sql.startswith("UPDATE sermon_events SET"):
            self.executed.append("update")
            event = self.events.get(params["event_id_1"])
            if event is None or event.user_id != params["user_id_1"]:
                return _FakeResult([])
            if "event_date" in params:
                event.event_date = params["event_date"]
            if "title" in params:
                event.title = params["title"]
            if "series" in params:
                event.series = params["series"]
            if "document_id" in params:
                event.document_id = params["document_id"]
            event.updated_at = self._now()
            return _FakeResult([tuple(getattr(event, c) for c in _UPDATE_TUPLE_COLS)])

        if "FROM documents" in sql:
            # The cross-table ownership pre-flight: owned (any deleted_at).
            self.executed.append("doc_gate")
            doc = self.docs.get(params["document_id_1"])
            if doc is None or doc.user_id != params["user_id_1"]:
                return _FakeResult([])
            return _FakeResult([doc.document_id])

        if "FROM sermon_events" in sql:
            if "event_id_1" in params:
                # Single-event owned gate (GET-full / PATCH gate).
                self.executed.append("gate")
                event = self.events.get(params["event_id_1"])
                if event is None or event.user_id != params["user_id_1"]:
                    return _FakeResult([])
                return _FakeResult([event])
            # The half-open range list.
            self.executed.append("range")
            start = params["event_date_1"]
            end = params["event_date_2"]
            rows = sorted(
                (
                    e
                    for e in self.events.values()
                    if e.user_id == params["user_id_1"]
                    and e.event_date >= start
                    and e.event_date < end  # noqa: SIM300 — mirrors the half-open SQL
                ),
                key=lambda e: e.event_date,
            )
            return _FakeResult(rows)

        msg = f"unexpected statement: {sql}"
        raise AssertionError(msg)


@pytest.fixture
def fake_user() -> _FakeUser:
    return _FakeUser()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, fake_user: _FakeUser) -> TestClient:
    """Dev-posture TestClient with auth overridden (test_documents_unit.py)."""
    monkeypatch.setattr(main_module.settings, "env", "dev")
    monkeypatch.setattr(main_module.settings, "jwt_secret", DEV_JWT_SECRET)
    monkeypatch.setitem(
        main_module.app.dependency_overrides,
        auth.get_current_user,
        lambda: fake_user,
    )
    return TestClient(main_module.app)


def _wire_session(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> None:
    async def _fake_session() -> Any:
        return session

    monkeypatch.setitem(main_module.app.dependency_overrides, auth._session, _fake_session)


# --- _weekly_dates (pure materializer arithmetic) ----------------------------


def test_weekly_dates_single_when_until_equals_start() -> None:
    d = date(2026, 6, 7)
    assert _weekly_dates(d, d) == [d]


def test_weekly_dates_steps_by_seven_inclusive() -> None:
    start = date(2026, 6, 7)  # a Sunday
    until = date(2026, 6, 28)
    assert _weekly_dates(start, until) == [
        date(2026, 6, 7),
        date(2026, 6, 14),
        date(2026, 6, 21),
        date(2026, 6, 28),
    ]


def test_weekly_dates_excludes_partial_week_past_until() -> None:
    # until lands 2 days before the next weekly step → that step is excluded.
    start = date(2026, 6, 7)
    until = date(2026, 6, 19)  # between the 14th and the 21st
    assert _weekly_dates(start, until) == [date(2026, 6, 7), date(2026, 6, 14)]


# --- statement compile pins (tenant audit) -----------------------------------


def test_range_stmt_is_user_scoped_and_half_open() -> None:
    user_id = uuid.uuid4()
    start, end = date(2026, 1, 1), date(2026, 12, 31)
    compiled = _range_stmt(user_id, start=start, end=end).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    # The load-bearing tenant filter + the half-open bounds (>= start, < end).
    assert "sermon_events.user_id =" in sql
    assert "sermon_events.event_date >=" in sql
    assert "sermon_events.event_date <" in sql
    assert "sermon_events.event_date <=" not in sql  # half-open, NOT closed
    assert "ORDER BY sermon_events.event_date" in sql
    assert user_id in compiled.params.values()
    assert {start, end} <= set(compiled.params.values())


def test_owned_event_stmt_is_doubly_scoped() -> None:
    event_id, user_id = uuid.uuid4(), uuid.uuid4()
    compiled = _owned_event_stmt(event_id, user_id).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "sermon_events.event_id =" in sql
    assert "sermon_events.user_id =" in sql
    assert set(compiled.params.values()) == {event_id, user_id}


def test_document_owned_stmt_is_user_scoped_without_deleted_predicate() -> None:
    document_id, user_id = uuid.uuid4(), uuid.uuid4()
    compiled = _document_owned_stmt(document_id, user_id).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    # Ownership is what matters — active OR soft-deleted both acceptable, so
    # NO deleted_at predicate; the user_id gate IS load-bearing.
    assert "documents.document_id =" in sql
    assert "documents.user_id =" in sql
    assert "deleted_at" not in sql
    assert set(compiled.params.values()) == {document_id, user_id}


def test_update_stmt_is_doubly_scoped_and_bumps_updated_at() -> None:
    event_id, user_id = uuid.uuid4(), uuid.uuid4()
    compiled = _update_stmt(
        event_id,
        user_id,
        values={"title": "X"},
    ).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith("UPDATE sermon_events SET")
    assert "sermon_events.event_id =" in sql
    assert "sermon_events.user_id =" in sql
    assert "updated_at=now()" in sql
    assert "RETURNING" in sql
    assert event_id in compiled.params.values()
    assert user_id in compiled.params.values()


def test_delete_stmt_is_doubly_scoped() -> None:
    event_id, user_id = uuid.uuid4(), uuid.uuid4()
    compiled = _delete_stmt(event_id, user_id).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith("DELETE FROM sermon_events")
    assert "sermon_events.event_id =" in sql
    assert "sermon_events.user_id =" in sql
    assert "RETURNING" in sql
    assert set(compiled.params.values()) == {event_id, user_id}


# --- POST + GET range round trip ---------------------------------------------


def _create(
    client: TestClient,
    *,
    event_date: str = "2026-06-07",
    title: str = "Sunday service",
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {"event_date": event_date, "title": title, **extra}
    resp = client.post("/calendar/events", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_single_event_persists_under_jwt_user(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        body = _create(client, title="Easter", series="Resurrection")

    assert len(body["events"]) == 1
    event = body["events"][0]
    assert event["title"] == "Easter"
    assert event["series"] == "Resurrection"
    assert event["event_date"] == "2026-06-07"
    assert event["document_id"] is None
    stored = session.events[uuid.UUID(event["event_id"])]
    assert stored.user_id == fake_user.user_id


def test_range_is_half_open_and_excludes_end(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        on_start = _create(client, event_date="2026-06-01", title="On start")
        middle = _create(client, event_date="2026-06-15", title="Middle")
        on_end = _create(client, event_date="2026-06-30", title="On end")
        resp = client.get("/calendar/events", params={"start": "2026-06-01", "end": "2026-06-30"})

    assert resp.status_code == 200, resp.text
    ids = [e["event_id"] for e in resp.json()["events"]]
    # start is INCLUDED, end is EXCLUDED (half-open).
    assert on_start["events"][0]["event_id"] in ids
    assert middle["events"][0]["event_id"] in ids
    assert on_end["events"][0]["event_id"] not in ids
    # Returned in event_date order.
    titles = [e["title"] for e in resp.json()["events"]]
    assert titles == ["On start", "Middle"]


def test_range_excludes_other_users_events(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    other = _FakeUser()
    other_id = uuid.uuid4()
    session = _FakeSession(
        events={
            other_id: _StoredEvent(
                event_id=other_id,
                user_id=other.user_id,
                event_date=date(2026, 6, 10),
                title="Theirs",
                series=None,
                document_id=None,
                created_at=datetime(2026, 6, 1, tzinfo=UTC),
                updated_at=datetime(2026, 6, 1, tzinfo=UTC),
            ),
        },
    )
    _wire_session(monkeypatch, session)

    with client:
        _create(client, event_date="2026-06-10", title="Mine")
        resp = client.get("/calendar/events", params={"start": "2026-06-01", "end": "2026-06-30"})

    titles = [e["title"] for e in resp.json()["events"]]
    assert titles == ["Mine"]


def test_range_start_after_end_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        resp = client.get("/calendar/events", params={"start": "2026-06-30", "end": "2026-06-01"})

    assert resp.status_code == 422


def test_range_span_at_cap_ok_over_cap_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    start = date(2026, 1, 1)
    at_cap = (start + timedelta(days=RANGE_CAP_DAYS)).isoformat()
    over_cap = (start + timedelta(days=RANGE_CAP_DAYS + 1)).isoformat()

    with client:
        ok = client.get("/calendar/events", params={"start": start.isoformat(), "end": at_cap})
        over = client.get("/calendar/events", params={"start": start.isoformat(), "end": over_cap})

    assert ok.status_code == 200, ok.text
    assert over.status_code == 422


# --- weekly materializer ------------------------------------------------------


def test_repeat_weekly_until_materializes_discrete_rows(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        body = _create(
            client,
            event_date="2026-06-07",
            title="Advent",
            series="Advent",
            repeat_weekly_until="2026-06-28",
        )

    events = body["events"]
    # 4 discrete weekly rows: 7th, 14th, 21st, 28th.
    assert [e["event_date"] for e in events] == [
        "2026-06-07",
        "2026-06-14",
        "2026-06-21",
        "2026-06-28",
    ]
    # Each is an INDEPENDENT row (distinct ids, no parent linkage).
    assert len({e["event_id"] for e in events}) == 4
    assert len(session.events) == 4


def test_repeat_weekly_at_cap_ok_over_cap_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    start = date(2026, 1, 4)  # a Sunday
    # Exactly MATERIALIZER_CAP_ROWS occurrences (the anchor + cap-1 steps).
    at_cap = (start + timedelta(weeks=MATERIALIZER_CAP_ROWS - 1)).isoformat()
    over_cap = (start + timedelta(weeks=MATERIALIZER_CAP_ROWS)).isoformat()

    with client:
        ok = _create(client, event_date=start.isoformat(), repeat_weekly_until=at_cap)
        over = client.post(
            "/calendar/events",
            json={
                "event_date": start.isoformat(),
                "title": "Too many",
                "repeat_weekly_until": over_cap,
            },
        )

    assert len(ok["events"]) == MATERIALIZER_CAP_ROWS
    assert over.status_code == 422
    # Only the accepted run persisted; the over-cap POST wrote nothing.
    assert len(session.events) == MATERIALIZER_CAP_ROWS


def test_repeat_weekly_until_before_event_date_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        resp = client.post(
            "/calendar/events",
            json={
                "event_date": "2026-06-14",
                "title": "Backwards",
                "repeat_weekly_until": "2026-06-07",
            },
        )

    assert resp.status_code == 422
    assert session.events == {}


def test_each_materialized_row_is_independently_patch_and_delete_able(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        body = _create(
            client,
            event_date="2026-06-07",
            title="Series",
            repeat_weekly_until="2026-06-21",
        )
        events = body["events"]
        assert len(events) == 3
        first, second, third = events

        # PATCH the middle row only — the others are untouched.
        patch = client.patch(
            f"/calendar/events/{second['event_id']}",
            json={"title": "Edited middle"},
        )
        assert patch.status_code == 200, patch.text
        assert patch.json()["title"] == "Edited middle"

        # DELETE the first row only.
        assert client.delete(f"/calendar/events/{first['event_id']}").status_code == 204

        # First gone; second edited; third intact and still GET-able.
        assert client.get(f"/calendar/events/{first['event_id']}").status_code == 404
        assert client.get(f"/calendar/events/{second['event_id']}").json()["title"] == (
            "Edited middle"
        )
        assert client.get(f"/calendar/events/{third['event_id']}").json()["title"] == "Series"

    assert len(session.events) == 2


# --- document_id ownership check (attacker-controlled body input) -------------


def test_create_with_owned_document_id_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    doc_id = uuid.uuid4()
    session = _FakeSession(docs={doc_id: _StoredDoc(document_id=doc_id, user_id=fake_user.user_id)})
    _wire_session(monkeypatch, session)

    with client:
        body = _create(client, document_id=str(doc_id))

    assert body["events"][0]["document_id"] == str(doc_id)


def test_create_with_soft_deleted_owned_document_id_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    # Ownership is what matters — a soft-deleted but owned doc is acceptable
    # (pre-made decision 4: no deleted_at filter on the ownership check).
    doc_id = uuid.uuid4()
    session = _FakeSession(
        docs={
            doc_id: _StoredDoc(
                document_id=doc_id,
                user_id=fake_user.user_id,
                deleted_at=datetime(2026, 6, 1, tzinfo=UTC),
            ),
        },
    )
    _wire_session(monkeypatch, session)

    with client:
        body = _create(client, document_id=str(doc_id))

    assert body["events"][0]["document_id"] == str(doc_id)


def test_create_with_other_users_document_id_is_404_no_oracle(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    other = _FakeUser()
    other_doc = uuid.uuid4()
    session = _FakeSession(
        docs={other_doc: _StoredDoc(document_id=other_doc, user_id=other.user_id)},
    )
    _wire_session(monkeypatch, session)

    with client:
        cross_tenant = client.post(
            "/calendar/events",
            json={"event_date": "2026-06-07", "title": "X", "document_id": str(other_doc)},
        )
        nonexistent = client.post(
            "/calendar/events",
            json={"event_date": "2026-06-07", "title": "X", "document_id": str(uuid.uuid4())},
        )

    # Both 404, byte-identical body — no oracle distinguishing "exists for
    # another user" from "does not exist".
    assert cross_tenant.status_code == 404
    assert nonexistent.status_code == 404
    assert cross_tenant.json() == nonexistent.json()
    # No event written on either rejected POST.
    assert session.events == {}
    assert "add" not in session.executed


def test_patch_relink_to_other_users_document_id_is_404(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    other = _FakeUser()
    other_doc = uuid.uuid4()
    session = _FakeSession(
        docs={other_doc: _StoredDoc(document_id=other_doc, user_id=other.user_id)},
    )
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        event_id = created["events"][0]["event_id"]
        resp = client.patch(
            f"/calendar/events/{event_id}",
            json={"document_id": str(other_doc)},
        )

    assert resp.status_code == 404
    # The event's link is untouched (no write on the 404 path).
    assert session.events[uuid.UUID(event_id)].document_id is None
    assert "update" not in session.executed


def test_patch_can_detach_document_id_with_null(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    doc_id = uuid.uuid4()
    session = _FakeSession(docs={doc_id: _StoredDoc(document_id=doc_id, user_id=fake_user.user_id)})
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client, document_id=str(doc_id))
        event_id = created["events"][0]["event_id"]
        assert created["events"][0]["document_id"] == str(doc_id)
        # Present-and-null = DETACH (no ownership check needed for null).
        resp = client.patch(f"/calendar/events/{event_id}", json={"document_id": None})

    assert resp.status_code == 200, resp.text
    assert resp.json()["document_id"] is None
    assert session.events[uuid.UUID(event_id)].document_id is None


# --- PATCH partial + posture --------------------------------------------------


def test_patch_event_date_reschedules(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client, event_date="2026-06-07")
        event_id = created["events"][0]["event_id"]
        # Drag-to-reschedule is just a PATCH of event_date.
        resp = client.patch(f"/calendar/events/{event_id}", json={"event_date": "2026-06-14"})

    assert resp.status_code == 200
    assert resp.json()["event_date"] == "2026-06-14"
    assert session.events[uuid.UUID(event_id)].event_date == date(2026, 6, 14)


def test_patch_empty_body_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        event_id = created["events"][0]["event_id"]
        resp = client.patch(f"/calendar/events/{event_id}", json={})

    assert resp.status_code == 422


def test_create_smuggled_user_id_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        resp = client.post(
            "/calendar/events",
            json={"event_date": "2026-06-07", "title": "X", "user_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 422
    assert session.added == []


def test_patch_smuggled_user_id_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        event_id = created["events"][0]["event_id"]
        resp = client.patch(
            f"/calendar/events/{event_id}",
            json={"title": "X", "user_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 422


# --- no-existence-oracle 404 matrix (cross-tenant event_id) -------------------


def test_cross_tenant_nonexistent_garbage_event_ids_are_identical_404s(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    """User B GET/PATCH/DELETE on user A's event, a nonexistent id, and a
    non-UUID id are byte-identical 404s — no existence oracle, write-free.
    """
    user_a = _FakeUser()
    a_event = uuid.uuid4()
    session = _FakeSession(
        events={
            a_event: _StoredEvent(
                event_id=a_event,
                user_id=user_a.user_id,
                event_date=date(2026, 6, 7),
                title="A's service",
                series=None,
                document_id=None,
                created_at=datetime(2026, 6, 1, tzinfo=UTC),
                updated_at=datetime(2026, 6, 1, tzinfo=UTC),
            ),
        },
    )
    _wire_session(monkeypatch, session)

    # fake_user is user B; A's event is not B's.
    with client:
        probes = [str(a_event), str(uuid.uuid4()), "not-a-uuid"]
        responses = [
            resp
            for probe in probes
            for resp in (
                client.get(f"/calendar/events/{probe}"),
                client.patch(f"/calendar/events/{probe}", json={"title": "hijack"}),
                client.delete(f"/calendar/events/{probe}"),
            )
        ]

    assert all(r.status_code == 404 for r in responses)
    assert len({r.text for r in responses}) == 1  # one body across routes + probe kinds
    # Nothing was COMMITTED on any 404 path — the doubly-scoped UPDATE/DELETE
    # match zero rows (the cross-tenant predicate excludes A's event) and the
    # handler 404s BEFORE committing, so A's event is untouched. (Like
    # documents.py DELETE, the scoped DELETE statement is still dispatched;
    # what matters is that it mutates nothing and never commits.)
    assert session.commits == 0
    assert a_event in session.events
    assert session.events[a_event].title == "A's service"
    assert fake_user.user_id != user_a.user_id
