"""Unit tests pinning the ``GET /library`` tenant filter + progress join — no DB.

The live query is exercised end-to-end by the Phase 15 API verify (and
the Phase 32 verify re-checks the join trap live); this file ensures:

- the WHERE clause stays scoped to the JWT-derived ``user_id`` and the
  join to ``global_books`` is present, so a refactor can't silently
  widen the listing to every tenant's books (CLAUDE.md tenant invariant);
- THE PHASE 32 TRAP: the ``reading_positions`` LEFT JOIN is ON
  (user_id AND book_id) — joining on ``book_id`` alone leaks another
  tenant's reading position for a shared deduped book (B1, verbatim);
- ``chunk_count`` is computed per request via a GROUP BY subquery over
  ``chunks`` (the in-phase decision: no denormalization onto
  ``global_books``);
- the ``_progress`` derivation and the route's response mapping,
  including two users sharing one deduped book each seeing ONLY their
  own position/progress (route-level, against a fake session that
  resolves rows the way the doubly-scoped join would — the live join
  itself is covered by the compile pin here + the Phase 32 verify).
"""

# ``_library_stmt`` / ``_progress`` are intentionally private;
# compiled-statement metadata (``.params``) is loosely typed under pyright
# strict, and route tests pass duck-typed fakes on purpose.
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
from library import _library_stmt, _progress
from settings import DEV_JWT_SECRET


def _compiled_sql(user_id: uuid.UUID) -> str:
    return str(_library_stmt(user_id).compile(dialect=postgresql.dialect()))


# --- statement compile pins ----------------------------------------------------


def test_library_stmt_filters_by_user_id() -> None:
    uid = uuid.uuid4()
    compiled = _library_stmt(uid).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    # The tenant gate: listing is scoped to user_library.user_id, and the
    # only bound value is the JWT-derived user_id we passed in.
    assert "user_library.user_id =" in sql
    assert uid in compiled.params.values()


def test_library_stmt_joins_global_books() -> None:
    # Title/author come from the shared global_books row; without the join
    # the listing can't render book names.
    sql = _compiled_sql(uuid.uuid4())
    assert "global_books" in sql
    assert "user_library" in sql


def test_library_stmt_orders_newest_first() -> None:
    sql = _compiled_sql(uuid.uuid4())
    assert "ORDER BY user_library.added_at DESC" in sql


def test_library_stmt_position_join_is_doubly_scoped() -> None:
    # THE PHASE 32 TRAP (B1, verbatim): the reading_positions join MUST be
    # ON (user_id AND book_id) — book_id alone leaks another tenant's
    # position for a shared deduped book. Pin the whole ON clause.
    sql = _compiled_sql(uuid.uuid4())
    assert (
        "LEFT OUTER JOIN reading_positions "
        "ON reading_positions.user_id = user_library.user_id "
        "AND reading_positions.book_id = user_library.book_id"
    ) in sql


def test_library_stmt_computes_chunk_count_per_request() -> None:
    # In-phase decision: chunk_count is a GROUP BY count subquery joined
    # per book (no denormalized column on global_books). The count join is
    # on book_id alone BY DESIGN — a chunk count is a property of the
    # shared deduped book, not of any tenant.
    sql = _compiled_sql(uuid.uuid4())
    assert "count(*) AS chunk_count" in sql
    assert "GROUP BY chunks.book_id" in sql
    # Outer join: a chunkless book must not drop off the listing.
    assert "LEFT OUTER JOIN (SELECT chunks.book_id" in sql


# --- progress derivation --------------------------------------------------------


@pytest.mark.parametrize(
    ("last_chunk_index", "chunk_count", "expected"),
    [
        (None, 100, None),  # no saved position
        (39, None, None),  # no countable chunks (left-join NULL)
        (39, 0, None),  # degenerate zero guard
        (0, 100, 0.01),  # first chunk read
        (39, 100, 0.4),
        (99, 100, 1.0),  # finished
        (150, 100, 1.0),  # stale position past a re-ingested shorter book: clamped
    ],
)
def test_progress_is_chunks_completed_over_total(
    last_chunk_index: int | None,
    chunk_count: int | None,
    expected: float | None,
) -> None:
    assert _progress(last_chunk_index, chunk_count) == expected


# --- route: per-user progress mapping (Phase 32) ---------------------------------

_ADDED_AT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


class _FakeUser:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()


class _FakeTupleResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def tuples(self) -> _FakeTupleResult:
        return self

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Duck-typed AsyncSession serving the 7-column library rows.

    Resolves the bound user_id from the compiled params (the
    ``test_uploads_unit.py`` philosophy) and assembles rows the way the
    doubly-scoped join would: positions are stored keyed by
    (user_id, book_id) and looked up with BOTH keys — so a book shared
    across tenants yields each caller ONLY their own position. (That the
    real statement joins on both columns is pinned by the compile test
    above; live behavior is the Phase 32 verify's job.)
    """

    def __init__(
        self,
        *,
        entries: list[tuple[uuid.UUID, uuid.UUID, str]],  # (user_id, book_id, title)
        chunk_counts: dict[uuid.UUID, int],
        positions: dict[tuple[uuid.UUID, uuid.UUID], int],
    ) -> None:
        self.entries = entries
        self.chunk_counts = chunk_counts
        self.positions = positions

    async def execute(self, stmt: Any) -> _FakeTupleResult:
        params = stmt.compile(dialect=postgresql.dialect()).params
        uid = params["user_id_1"]
        rows = [
            (
                book_id,
                title,
                None,  # author
                None,  # category
                _ADDED_AT,
                self.chunk_counts.get(book_id),
                self.positions.get((user_id, book_id)),
            )
            for user_id, book_id, title in self.entries
            if user_id == uid
        ]
        return _FakeTupleResult(rows)


def test_two_users_sharing_a_book_each_see_only_their_own_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_a, user_b = _FakeUser(), _FakeUser()
    shared_book = uuid.uuid4()
    session = _FakeSession(
        entries=[
            (user_a.user_id, shared_book, "Institutes"),
            (user_b.user_id, shared_book, "Institutes"),
        ],
        chunk_counts={shared_book: 100},
        positions={
            (user_a.user_id, shared_book): 9,
            (user_b.user_id, shared_book): 49,
        },
    )

    monkeypatch.setattr(main_module.settings, "env", "dev")
    monkeypatch.setattr(main_module.settings, "jwt_secret", DEV_JWT_SECRET)
    current: dict[str, _FakeUser] = {"user": user_a}
    monkeypatch.setitem(
        main_module.app.dependency_overrides,
        auth.get_current_user,
        lambda: current["user"],
    )

    async def _fake_session() -> Any:
        return session

    monkeypatch.setitem(main_module.app.dependency_overrides, auth._session, _fake_session)

    with TestClient(main_module.app) as client:
        as_a = client.get("/library")
        current["user"] = user_b
        as_b = client.get("/library")

    assert as_a.status_code == as_b.status_code == 200
    (book_a,) = as_a.json()["books"]
    (book_b,) = as_b.json()["books"]
    # Same deduped book, same chunk_count — but each tenant sees ONLY
    # their own position and progress.
    assert book_a["book_id"] == book_b["book_id"] == str(shared_book)
    assert book_a["chunk_count"] == book_b["chunk_count"] == 100
    assert book_a["last_chunk_index"] == 9
    assert book_a["progress"] == 0.1
    assert book_b["last_chunk_index"] == 49
    assert book_b["progress"] == 0.5
    # Backward-compatible shape: the Phase 15 fields are all still there.
    assert {"book_id", "title", "author", "category", "added_at"} <= book_a.keys()


def test_library_row_without_position_has_null_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _FakeUser()
    book = uuid.uuid4()
    session = _FakeSession(
        entries=[(user.user_id, book, "Confessions")],
        chunk_counts={book: 80},
        positions={},
    )

    monkeypatch.setattr(main_module.settings, "env", "dev")
    monkeypatch.setattr(main_module.settings, "jwt_secret", DEV_JWT_SECRET)
    monkeypatch.setitem(
        main_module.app.dependency_overrides,
        auth.get_current_user,
        lambda: user,
    )

    async def _fake_session() -> Any:
        return session

    monkeypatch.setitem(main_module.app.dependency_overrides, auth._session, _fake_session)

    with TestClient(main_module.app) as client:
        response = client.get("/library")

    (row,) = response.json()["books"]
    assert row["chunk_count"] == 80  # count is a property of the book
    assert row["last_chunk_index"] is None  # never opened it
    assert row["progress"] is None
