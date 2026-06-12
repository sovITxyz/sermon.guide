"""Unit tests for the reader routes (Phase 32 contract).

Pure-unit, no live infra. Route tests boot ``main.app`` through
``with TestClient(app):`` (lifespan runs; dev posture monkeypatched as
settings ATTRIBUTES, the suite convention) and replace auth + the DB
session via ``app.dependency_overrides`` (the ``test_uploads_unit.py``
pattern). The fake session resolves statements the way the DB would —
routing on the compiled SQL, predicates from the compiled params — and
keys its position store on (user_id, book_id), the
``uq_reading_positions_user_book`` conflict target, so the upsert
branch demonstrates one-row-per-(user, book) semantics. The real ON
CONFLICT clause itself is compile-pinned below; live behavior is the
Phase 32 verify stage's job.

What this file pins:

- the window contract: default 40, ``?limit=500`` silently capped at
  100 (the Phase 32 contract — NOT a 422), ``start`` past the end is an
  empty 200, malformed ``start``/``limit`` (negative / non-positive)
  are 422s per the Phase 18 fail-loud posture;
- the no-existence-oracle 404 matrix on BOTH ``/chunks`` and
  ``/position`` (GET + PUT): non-owned, nonexistent, and non-UUID
  garbage book ids are byte-identical 404s, and NO chunk/position
  query runs on the 404 path (gate-before-read ordering);
- PUT /position upserts: twice → ONE row (latest values win), an
  omitted ``offset_ratio`` clears the stored one (full-replace
  semantics), a smuggled extra body field is a hard 422, out-of-range
  ``chunk_index``/``offset_ratio`` are 422s;
- GET /position with no saved row → 200 with all-null fields (never a
  404 — that's reserved for the ownership gate);
- every statement builder carries its tenant predicates (the
  ``test_library_unit.py`` compile-pin pattern): membership filters by
  BOTH book_id and the JWT user_id, the position lookup and upsert are
  doubly-scoped, and the upsert targets the unique constraint.
"""

# Tests exercise module-internals and pass duck-typed fakes on purpose.
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportArgumentType=false

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

import auth
import main as main_module
from reader import (
    _chunk_window_stmt,
    _membership_stmt,
    _position_stmt,
    _position_upsert_stmt,
)
from settings import DEV_JWT_SECRET

_NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)


# --- fakes -------------------------------------------------------------------


class _FakeUser:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()


class _FakeResult:
    """Covers every consumption style the reader routes use."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def tuples(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return self._rows

    def one_or_none(self) -> Any:
        assert len(self._rows) <= 1
        return self._rows[0] if self._rows else None

    def one(self) -> Any:
        assert len(self._rows) == 1
        return self._rows[0]


class _FakeSession:
    """Duck-typed AsyncSession resolving statements the way the DB would.

    Statements are routed on their compiled SQL and resolved from the
    compiled params (the ``test_uploads_unit.py`` philosophy). Store:

    - ``library`` — set of (user_id, book_id) membership pairs;
    - ``chunk_counts`` — book_id → dense chunk count (indices 0..N-1);
    - ``positions`` — keyed by (user_id, book_id), the
      ``uq_reading_positions_user_book`` conflict target, so the INSERT
      branch upserts exactly like the constraint would.

    ``executed`` records the statement kinds in order so tests can pin
    that the membership gate runs FIRST and nothing else runs on the
    404 path.
    """

    def __init__(
        self,
        *,
        library: set[tuple[uuid.UUID, uuid.UUID]] | None = None,
        chunk_counts: dict[uuid.UUID, int] | None = None,
    ) -> None:
        self.library = library or set()
        self.chunk_counts = chunk_counts or {}
        self.positions: dict[
            tuple[uuid.UUID, uuid.UUID],
            tuple[int, float | None, datetime],
        ] = {}
        self.executed: list[str] = []
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def execute(self, stmt: Any) -> _FakeResult:
        compiled = stmt.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        params = compiled.params
        if sql.startswith("INSERT INTO reading_positions"):
            self.executed.append("upsert")
            key = (params["user_id"], params["book_id"])
            self.positions[key] = (params["chunk_index"], params["offset_ratio"], _NOW)
            return _FakeResult([self.positions[key]])
        if "FROM user_library" in sql:
            self.executed.append("membership")
            key = (params["user_id_1"], params["book_id_1"])
            return _FakeResult([params["book_id_1"]] if key in self.library else [])
        if "FROM reading_positions" in sql:
            self.executed.append("position")
            key = (params["user_id_1"], params["book_id_1"])
            row = self.positions.get(key)
            return _FakeResult([row] if row is not None else [])
        if "FROM chunks" in sql:
            self.executed.append("chunks")
            book_id = params["book_id_1"]
            start, limit = params["chunk_index_1"], params["param_1"]
            count = self.chunk_counts.get(book_id, 0)
            rows = [(i, f"chunk {i}") for i in range(start, min(start + limit, count))]
            return _FakeResult(rows)
        msg = f"unexpected statement: {sql}"
        raise AssertionError(msg)


@pytest.fixture
def fake_user() -> _FakeUser:
    return _FakeUser()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, fake_user: _FakeUser) -> TestClient:
    """Dev-posture TestClient with auth overridden (test_uploads_unit.py)."""
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


def _owned_book(fake_user: _FakeUser, *, chunk_count: int = 150) -> tuple[uuid.UUID, _FakeSession]:
    book_id = uuid.uuid4()
    session = _FakeSession(
        library={(fake_user.user_id, book_id)},
        chunk_counts={book_id: chunk_count},
    )
    return book_id, session


# --- GET /books/{book_id}/chunks — window contract ----------------------------


def test_chunks_default_window_is_40(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    book_id, session = _owned_book(fake_user, chunk_count=150)
    _wire_session(monkeypatch, session)

    with client:
        response = client.get(f"/books/{book_id}/chunks")

    assert response.status_code == 200
    body = response.json()
    assert body["book_id"] == str(book_id)
    assert len(body["chunks"]) == 40
    assert [c["chunk_index"] for c in body["chunks"]] == list(range(40))
    assert body["chunks"][0]["content"] == "chunk 0"
    # Gate-before-read: membership resolved BEFORE the chunk window ran.
    assert session.executed == ["membership", "chunks"]


def test_chunks_limit_500_is_silently_capped_at_100(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    # Phase 32 contract: an over-ask is capped, NOT a 422 (deliberate
    # divergence from SearchRequest's le=100 body validation).
    book_id, session = _owned_book(fake_user, chunk_count=150)
    _wire_session(monkeypatch, session)

    with client:
        response = client.get(f"/books/{book_id}/chunks", params={"limit": 500})

    assert response.status_code == 200
    assert len(response.json()["chunks"]) == 100


def test_chunks_start_past_end_is_empty_200(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    book_id, session = _owned_book(fake_user, chunk_count=150)
    _wire_session(monkeypatch, session)

    with client:
        response = client.get(f"/books/{book_id}/chunks", params={"start": 150})

    assert response.status_code == 200
    assert response.json()["chunks"] == []


def test_chunks_start_windows_into_the_tail(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    book_id, session = _owned_book(fake_user, chunk_count=150)
    _wire_session(monkeypatch, session)

    with client:
        response = client.get(f"/books/{book_id}/chunks", params={"start": 145, "limit": 40})

    assert response.status_code == 200
    assert [c["chunk_index"] for c in response.json()["chunks"]] == [145, 146, 147, 148, 149]


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": -5}, {"start": -1}])
def test_chunks_malformed_window_params_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
    params: dict[str, int],
) -> None:
    # Negative start / non-positive limit are malformed input, not an
    # over-ask — hard 422 per the Phase 18 fail-loud posture.
    book_id, session = _owned_book(fake_user)
    _wire_session(monkeypatch, session)

    with client:
        response = client.get(f"/books/{book_id}/chunks", params=params)

    assert response.status_code == 422


# --- the 404 matrix: no existence oracle on /chunks or /position --------------


def test_non_owned_nonexistent_and_garbage_are_identical_404s(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    """Cross-tenant, unknown, and non-UUID book ids: byte-identical 404s.

    The shared book IS in another tenant's library — exactly the case
    where a 403 (or any different body) would leak its existence.
    """
    other_user = _FakeUser()
    shared_book = uuid.uuid4()
    session = _FakeSession(
        library={(other_user.user_id, shared_book)},
        chunk_counts={shared_book: 150},
    )
    _wire_session(monkeypatch, session)

    probes = [str(shared_book), str(uuid.uuid4()), "not-a-uuid"]
    with client:
        responses = [
            resp
            for probe in probes
            for resp in (
                client.get(f"/books/{probe}/chunks"),
                client.get(f"/books/{probe}/position"),
                client.put(f"/books/{probe}/position", json={"chunk_index": 3}),
            )
        ]

    assert all(r.status_code == 404 for r in responses)
    bodies = {r.text for r in responses}
    assert len(bodies) == 1  # no existence oracle across routes OR probe kinds
    # Nothing past the gate ran: no chunk reads, no position reads, no
    # upsert — and the cross-tenant PUT created nothing.
    assert "chunks" not in session.executed
    assert "position" not in session.executed
    assert "upsert" not in session.executed
    assert session.positions == {}


# --- GET/PUT /books/{book_id}/position ----------------------------------------


def test_position_get_without_saved_row_is_null_shape(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    # "No position yet" is a 200 with null fields (the TaskStatusResponse
    # nullable-result precedent) — 404 is reserved for the ownership gate.
    book_id, session = _owned_book(fake_user)
    _wire_session(monkeypatch, session)

    with client:
        response = client.get(f"/books/{book_id}/position")

    assert response.status_code == 200
    assert response.json() == {
        "book_id": str(book_id),
        "chunk_index": None,
        "offset_ratio": None,
        "updated_at": None,
    }


def test_position_put_then_get_roundtrips(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    book_id, session = _owned_book(fake_user)
    _wire_session(monkeypatch, session)

    with client:
        put = client.put(
            f"/books/{book_id}/position",
            json={"chunk_index": 12, "offset_ratio": 0.5},
        )
        got = client.get(f"/books/{book_id}/position")

    assert put.status_code == 200
    assert got.status_code == 200
    for body in (put.json(), got.json()):
        assert body["book_id"] == str(book_id)
        assert body["chunk_index"] == 12
        assert body["offset_ratio"] == 0.5
        assert body["updated_at"] is not None
    # The route committed the upsert.
    assert session.commits == 1


def test_position_put_twice_upserts_one_row(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    book_id, session = _owned_book(fake_user)
    _wire_session(monkeypatch, session)

    with client:
        first = client.put(f"/books/{book_id}/position", json={"chunk_index": 10})
        second = client.put(
            f"/books/{book_id}/position",
            json={"chunk_index": 99, "offset_ratio": 0.25},
        )

    assert first.status_code == second.status_code == 200
    # ONE row per (user, book) — the second PUT updated in place.
    assert len(session.positions) == 1
    assert session.positions[(fake_user.user_id, book_id)][:2] == (99, 0.25)


def test_position_put_omitted_offset_ratio_clears_it(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    """Full-replace semantics: a PUT without offset_ratio stores NULL.

    A ratio measured inside chunk 12 is meaningless against chunk 99 —
    the PUT states the whole position, it is not a patch.
    """
    book_id, session = _owned_book(fake_user)
    _wire_session(monkeypatch, session)

    with client:
        client.put(f"/books/{book_id}/position", json={"chunk_index": 12, "offset_ratio": 0.7})
        client.put(f"/books/{book_id}/position", json={"chunk_index": 99})
        got = client.get(f"/books/{book_id}/position")

    assert got.json()["chunk_index"] == 99
    assert got.json()["offset_ratio"] is None


def test_position_put_smuggled_field_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    # extra="forbid" (Phase 18): a smuggled user_id must fail loud, never
    # be a silently-dropped key — tenant invariant made mechanical.
    book_id, session = _owned_book(fake_user)
    _wire_session(monkeypatch, session)

    with client:
        response = client.put(
            f"/books/{book_id}/position",
            json={"chunk_index": 3, "user_id": str(uuid.uuid4())},
        )

    assert response.status_code == 422
    assert session.positions == {}


@pytest.mark.parametrize(
    "body",
    [
        {"chunk_index": -1},
        {"chunk_index": 3, "offset_ratio": 1.5},
        {"chunk_index": 3, "offset_ratio": -0.1},
        {},
    ],
)
def test_position_put_out_of_range_body_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
    body: dict[str, Any],
) -> None:
    book_id, session = _owned_book(fake_user)
    _wire_session(monkeypatch, session)

    with client:
        response = client.put(f"/books/{book_id}/position", json=body)

    assert response.status_code == 422
    assert session.positions == {}


# --- statement compile pins (tenant audit) -------------------------------------


def test_membership_stmt_filters_by_book_and_user() -> None:
    book_id, user_id = uuid.uuid4(), uuid.uuid4()
    compiled = _membership_stmt(book_id, user_id).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    # Both predicates are load-bearing: chunks has no user_id column, so
    # this statement is the ONLY tenant gate on the reader surface — drop
    # user_id and any authenticated user reads any ingested book.
    assert "user_library.book_id =" in sql
    assert "user_library.user_id =" in sql
    assert set(compiled.params.values()) == {book_id, user_id}


def test_chunk_window_stmt_is_book_scoped_ordered_and_limited() -> None:
    book_id = uuid.uuid4()
    compiled = _chunk_window_stmt(book_id, start=20, limit=40).compile(
        dialect=postgresql.dialect(),
    )
    sql = str(compiled)
    assert "chunks.book_id =" in sql
    assert "chunks.chunk_index >=" in sql
    assert "ORDER BY chunks.chunk_index ASC" in sql
    assert "LIMIT" in sql
    assert book_id in compiled.params.values()
    assert 20 in compiled.params.values()
    assert 40 in compiled.params.values()


def test_position_stmt_is_doubly_scoped() -> None:
    book_id, user_id = uuid.uuid4(), uuid.uuid4()
    compiled = _position_stmt(book_id, user_id).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    # The highlights invariant: user_id AND book_id — book_id alone would
    # serve another tenant's resume point for a shared deduped book.
    assert "reading_positions.book_id =" in sql
    assert "reading_positions.user_id =" in sql
    assert set(compiled.params.values()) == {book_id, user_id}


def test_position_upsert_stmt_targets_unique_constraint_with_jwt_user() -> None:
    user_id, book_id = uuid.uuid4(), uuid.uuid4()
    compiled = _position_upsert_stmt(
        user_id=user_id,
        book_id=book_id,
        chunk_index=7,
        offset_ratio=0.25,
    ).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith("INSERT INTO reading_positions")
    # Upsert on the ONE-row-per-(user, book) constraint, replacing the
    # position wholesale and bumping updated_at explicitly (the column
    # has server_default but no onupdate).
    assert "ON CONFLICT ON CONSTRAINT uq_reading_positions_user_book DO UPDATE" in sql
    assert "chunk_index = excluded.chunk_index" in sql
    assert "offset_ratio = excluded.offset_ratio" in sql
    assert "updated_at = now()" in sql
    assert "RETURNING" in sql
    assert compiled.params["user_id"] == user_id
    assert compiled.params["book_id"] == book_id
    assert compiled.params["chunk_index"] == 7
    assert compiled.params["offset_ratio"] == 0.25
