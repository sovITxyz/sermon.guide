"""Unit tests for the search-history routes + the save-on-summary hook (Phase 51).

Pure-unit, no live infra — the ``test_collections_unit.py`` template. Route
tests boot ``main.app`` through ``with TestClient(app):`` (lifespan runs; dev
posture monkeypatched as settings ATTRIBUTES) and replace auth + the DB session
via ``app.dependency_overrides``. The fake session resolves statements the way
the DB would — routing on the compiled SQL, predicates from the compiled params
— keying a history store on ``history_id`` and honoring the newest-first list,
the no-oracle single-row gate, the per-id / clear-all deletes, and the
retention-cap prune.

What this file pins:

- the statement builders (the mechanical tenant audit, no DB): ``_list_stmt``
  carries ``user_id`` + ORDER BY ``created_at`` DESC + projects only a summary
  preview (no citations blob); ``_owned_history_stmt`` / ``_delete_stmt`` are
  doubly-scoped (``history_id`` AND ``user_id``); ``_clear_all_stmt`` carries
  ``user_id``; ``_prune_stmt`` is user-scoped (inner AND outer) and OFFSETs past
  the newest ``keep``;
- save-on-summary: a successful ``search_summary`` inserts EXACTLY ONE history
  row scoped to the JWT user, carrying the query + scope + serialized result;
- the retention cap: ``record_search_history`` prunes the user's rows beyond the
  newest ``SEARCH_HISTORY_RETENTION``, dropping the OLDEST;
- the read/delete round trips: list is newest-first with a preview; GET-full
  returns the whole ``result``; per-id + clear-all deletes work;
- a smuggled ``user_id`` on the ``/search-summary`` body (the only inbound body
  feeding the save path) is a hard 422, write-free;
- the no-existence-oracle 404 matrix: cross-tenant, nonexistent, and non-UUID
  garbage history ids are byte-identical 404s on GET / DELETE, write-free.
"""

# Tests exercise module-internals and pass duck-typed fakes on purpose.
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportArgumentType=false

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

import auth
import main as main_module
import search_history as history_module
import summary as summary_module
from search import SearchHit, SearchOutcome
from search_history import (
    SEARCH_HISTORY_RETENTION,
    _clear_all_stmt,
    _delete_stmt,
    _list_stmt,
    _owned_history_stmt,
    _prune_stmt,
    record_search_history,
)
from settings import DEV_JWT_SECRET

# --- fakes -------------------------------------------------------------------


class _FakeUser:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()


class _StoredHistory:
    """A row in the fake search_history table — duck-types the ORM attributes."""

    def __init__(
        self,
        *,
        history_id: uuid.UUID,
        user_id: uuid.UUID,
        query: str,
        scope_book_ids: list[str],
        scope_collection_ids: list[str],
        result: dict[str, Any],
        created_at: datetime,
    ) -> None:
        self.history_id = history_id
        self.user_id = user_id
        self.query = query
        self.scope_book_ids = scope_book_ids
        self.scope_collection_ids = scope_collection_ids
        self.result = result
        self.created_at = created_at


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

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Duck-typed AsyncSession resolving statements the way the DB would.

    Statements route on their compiled SQL and resolve from the compiled params
    (the ``test_collections_unit.py`` philosophy). ``history`` is keyed by
    ``history_id``. ``executed`` records statement kinds in order so a 404 path
    can be proven write-free.
    """

    def __init__(self, *, history: dict[uuid.UUID, _StoredHistory] | None = None) -> None:
        self.history: dict[uuid.UUID, _StoredHistory] = history or {}
        self.added: list[Any] = []
        self.executed: list[str] = []
        self.commits = 0
        self.flushes = 0
        self._clock = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)

    def _now(self) -> datetime:
        self._clock += timedelta(seconds=1)
        return self._clock

    def add(self, obj: Any) -> None:
        self.executed.append("add")
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1
        # Materialize any pending ORM inserts into the store (the DB would assign
        # the PK default + server_default created_at here).
        for obj in self.added:
            history_id = getattr(obj, "history_id", None) or uuid.uuid4()
            created_at = getattr(obj, "created_at", None) or self._now()
            self.history[history_id] = _StoredHistory(
                history_id=history_id,
                user_id=obj.user_id,
                query=obj.query,
                scope_book_ids=list(obj.scope_book_ids),
                scope_collection_ids=list(obj.scope_collection_ids),
                result=dict(obj.result),
                created_at=created_at,
            )
        self.added = []

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.added = []

    async def execute(self, stmt: Any) -> _FakeResult:
        compiled = stmt.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        params = compiled.params

        if sql.startswith("DELETE FROM search_history"):
            if "IN (SELECT" in sql:
                # _prune_stmt — keep the newest SEARCH_HISTORY_RETENTION, drop
                # the rest (read the module global so a monkeypatch is honored).
                self.executed.append("prune")
                user_id = params["user_id_1"]
                keep = history_module.SEARCH_HISTORY_RETENTION
                owned = sorted(
                    (h for h in self.history.values() if h.user_id == user_id),
                    key=lambda h: h.created_at,
                    reverse=True,
                )
                removed: list[uuid.UUID] = []
                for stale in owned[keep:]:
                    del self.history[stale.history_id]
                    removed.append(stale.history_id)
                return _FakeResult(removed)
            if "search_history.history_id =" in sql:
                # _delete_stmt — the per-id gate.
                self.executed.append("delete")
                row = self.history.get(params["history_id_1"])
                if row is None or row.user_id != params["user_id_1"]:
                    return _FakeResult([])
                del self.history[row.history_id]
                return _FakeResult([row.history_id])
            # _clear_all_stmt — wipe the user's rows.
            self.executed.append("clear")
            user_id = params["user_id_1"]
            cleared = [h.history_id for h in self.history.values() if h.user_id == user_id]
            for hid in cleared:
                del self.history[hid]
            return _FakeResult(cleared)

        if sql.startswith("SELECT"):
            user_id = params["user_id_1"]
            if "jsonb_extract_path_text" in sql:
                # _list_stmt — newest-first lightweight rows.
                self.executed.append("list")
                rows = sorted(
                    (h for h in self.history.values() if h.user_id == user_id),
                    key=lambda h: h.created_at,
                    reverse=True,
                )
                return _FakeResult(
                    [
                        (
                            h.history_id,
                            h.query,
                            h.scope_book_ids,
                            h.scope_collection_ids,
                            h.created_at,
                            h.result.get("summary"),
                        )
                        for h in rows
                    ],
                )
            # _owned_history_stmt — the single-row gate.
            self.executed.append("gate")
            row = self.history.get(params["history_id_1"])
            if row is None or row.user_id != user_id:
                return _FakeResult([])
            return _FakeResult([row])

        msg = f"unexpected statement: {sql}"
        raise AssertionError(msg)


@pytest.fixture
def fake_user() -> _FakeUser:
    return _FakeUser()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, fake_user: _FakeUser) -> TestClient:
    """Dev-posture TestClient with auth + the summary rate-limit overridden."""
    monkeypatch.setattr(main_module.settings, "env", "dev")
    monkeypatch.setattr(main_module.settings, "jwt_secret", DEV_JWT_SECRET)
    monkeypatch.setitem(
        main_module.app.dependency_overrides,
        auth.get_current_user,
        lambda: fake_user,
    )
    # The /search-summary save path rides the per-user limiter; stub it so the
    # smuggled-field test never needs a live Redis.
    monkeypatch.setitem(
        main_module.app.dependency_overrides,
        summary_module._summary_rate_limit,
        lambda: None,
    )
    return TestClient(main_module.app)


def _wire_session(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> None:
    async def _fake_session() -> Any:
        return session

    monkeypatch.setitem(main_module.app.dependency_overrides, auth._session, _fake_session)


# --- statement compile pins (tenant audit) -----------------------------------


def test_list_stmt_is_user_scoped_and_lightweight() -> None:
    user_id = uuid.uuid4()
    compiled = _list_stmt(user_id).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "search_history.user_id =" in sql
    assert "ORDER BY search_history.created_at DESC" in sql
    # Projects only a summary preview server-side — never the full result blob.
    assert "jsonb_extract_path_text" in sql
    assert user_id in compiled.params.values()


def test_owned_history_stmt_is_doubly_scoped() -> None:
    history_id, user_id = uuid.uuid4(), uuid.uuid4()
    compiled = _owned_history_stmt(history_id, user_id).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "search_history.history_id =" in sql
    assert "search_history.user_id =" in sql
    assert set(compiled.params.values()) == {history_id, user_id}


def test_delete_stmt_is_doubly_scoped() -> None:
    history_id, user_id = uuid.uuid4(), uuid.uuid4()
    compiled = _delete_stmt(history_id, user_id).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith("DELETE FROM search_history")
    assert "search_history.history_id =" in sql
    assert "search_history.user_id =" in sql
    assert "RETURNING" in sql
    assert set(compiled.params.values()) == {history_id, user_id}


def test_clear_all_stmt_is_user_scoped() -> None:
    user_id = uuid.uuid4()
    compiled = _clear_all_stmt(user_id).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith("DELETE FROM search_history")
    assert "search_history.user_id =" in sql
    assert user_id in compiled.params.values()


def test_prune_stmt_is_user_scoped_inside_and_out() -> None:
    user_id = uuid.uuid4()
    compiled = _prune_stmt(user_id, keep=100).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith("DELETE FROM search_history")
    # The subquery + the outer DELETE each carry the user_id predicate.
    assert sql.count("search_history.user_id =") == 2
    assert "OFFSET" in sql
    assert "search_history.history_id IN (SELECT" in sql
    # Only the JWT user appears in the params (besides the offset literal).
    assert user_id in compiled.params.values()


# --- save-on-summary ---------------------------------------------------------


def _hit(book_int: int, chunk_index: int, *, content: str = "grace") -> SearchHit:
    return SearchHit(
        book_id=uuid.UUID(int=book_int),
        content_chunk=content,
        metadata={"chunk_index": chunk_index, "filename": "book.epub"},
        score=1.0,
    )


async def test_successful_summary_writes_one_scoped_history_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful summary inserts EXACTLY ONE history row scoped to the JWT
    user, carrying the query + chosen scope + the serialized result."""
    monkeypatch.setattr(summary_module.settings, "llm_provider", "google")
    monkeypatch.setattr(summary_module.settings, "google_api_key", "k")
    b1 = uuid.UUID(int=1)
    bid, cid = uuid.uuid4(), uuid.uuid4()

    async def _fake_run_search(**_: Any) -> SearchOutcome:
        return SearchOutcome(hits=[_hit(1, 7)], degraded=[])

    async def _fake_resolve_titles(_session: Any, _book_ids: Any) -> dict[uuid.UUID, str]:
        return {b1: "Romans"}

    def _fake_gen(**_: Any) -> str:
        return "Grace is central [Romans:7]."

    monkeypatch.setattr(summary_module, "run_search", _fake_run_search)
    monkeypatch.setattr(summary_module, "_resolve_titles", _fake_resolve_titles)
    monkeypatch.setattr(summary_module, "_generate_summary", _fake_gen)

    user = _FakeUser()
    session = _FakeSession()
    user_arg: Any = user
    session_arg: Any = session

    resp = await summary_module.search_summary(
        payload=summary_module.SummaryRequest(
            query="grace?",
            book_ids=[bid],
            collection_ids=[cid],
        ),
        current_user=user_arg,
        session=session_arg,
    )

    assert resp.summary == "Grace is central [Romans:7]."
    # Exactly one row, scoped to the JWT user, carrying the query + scope.
    assert len(session.history) == 1
    stored = next(iter(session.history.values()))
    assert stored.user_id == user.user_id
    assert stored.query == "grace?"
    assert stored.scope_book_ids == [str(bid)]
    assert stored.scope_collection_ids == [str(cid)]
    # The serialized result replays the whole SummaryResponse.
    assert stored.result["summary"] == resp.summary
    assert stored.result["degraded"] == []
    assert [c["marker"] for c in stored.result["citations"]] == ["[Romans:7]"]
    assert session.commits == 1


async def test_no_context_summary_still_records_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty-retrieval summary (the deterministic no-context message) is also
    saved — the costly retrieval still ran, so it is replayable."""
    monkeypatch.setattr(summary_module.settings, "llm_provider", "google")
    monkeypatch.setattr(summary_module.settings, "google_api_key", "k")

    async def _fake_run_search(**_: Any) -> SearchOutcome:
        return SearchOutcome(hits=[], degraded=[])

    monkeypatch.setattr(summary_module, "run_search", _fake_run_search)

    user = _FakeUser()
    session = _FakeSession()
    user_arg: Any = user
    session_arg: Any = session

    resp = await summary_module.search_summary(
        payload=summary_module.SummaryRequest(query="nothing here"),
        current_user=user_arg,
        session=session_arg,
    )

    assert resp.summary == summary_module._NO_CONTEXT_MESSAGE
    assert len(session.history) == 1
    stored = next(iter(session.history.values()))
    assert stored.user_id == user.user_id
    assert stored.scope_book_ids == []
    assert stored.scope_collection_ids == []


# --- retention cap -----------------------------------------------------------


async def test_record_prunes_oldest_beyond_retention_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``record_search_history`` keeps only the newest ``SEARCH_HISTORY_RETENTION``
    rows per user, dropping the OLDEST in the same transaction as the insert."""
    monkeypatch.setattr(history_module, "SEARCH_HISTORY_RETENTION", 3)
    user = _FakeUser()
    session = _FakeSession()
    session_arg: Any = session

    queries = ["q0", "q1", "q2", "q3", "q4"]
    for q in queries:
        await record_search_history(
            session_arg,
            user_id=user.user_id,
            query=q,
            scope_book_ids=None,
            scope_collection_ids=None,
            result={"summary": q, "citations": [], "degraded": []},
        )

    # Capped at 3, and the survivors are the THREE NEWEST (q2/q3/q4).
    assert len(session.history) == 3
    surviving = {h.query for h in session.history.values()}
    assert surviving == {"q2", "q3", "q4"}


async def test_record_is_best_effort_swallows_failure() -> None:
    """A history-write failure is swallowed — ``record_search_history`` never
    raises (the summary it follows must not 5xx)."""

    class _BoomSession:
        def __init__(self) -> None:
            self.rolled_back = False

        def add(self, _obj: Any) -> None:
            msg = "insert boom"
            raise RuntimeError(msg)

        async def rollback(self) -> None:
            self.rolled_back = True

    boom = _BoomSession()
    session_arg: Any = boom

    # Must NOT raise.
    await record_search_history(
        session_arg,
        user_id=uuid.uuid4(),
        query="q",
        scope_book_ids=None,
        scope_collection_ids=None,
        result={"summary": "q", "citations": [], "degraded": []},
    )
    assert boom.rolled_back is True


# --- read / delete round trips -----------------------------------------------


def _seed(user_id: uuid.UUID, *, query: str, when: datetime) -> _StoredHistory:
    return _StoredHistory(
        history_id=uuid.uuid4(),
        user_id=user_id,
        query=query,
        scope_book_ids=[],
        scope_collection_ids=[],
        result={"summary": f"summary for {query}", "citations": [], "degraded": []},
        created_at=when,
    )


def test_list_returns_only_jwt_users_rows_newest_first_with_preview(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    other = _FakeUser()
    older = _seed(fake_user.user_id, query="older", when=datetime(2026, 6, 1, tzinfo=UTC))
    newer = _seed(fake_user.user_id, query="newer", when=datetime(2026, 6, 2, tzinfo=UTC))
    theirs = _seed(other.user_id, query="theirs", when=datetime(2026, 6, 3, tzinfo=UTC))
    session = _FakeSession(
        history={h.history_id: h for h in (older, newer, theirs)},
    )
    _wire_session(monkeypatch, session)

    with client:
        resp = client.get("/search-history")

    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    # Only the JWT user's rows, newest-first.
    assert [i["query"] for i in items] == ["newer", "older"]
    # The lightweight preview carries the summary text, no citations key.
    assert items[0]["summary_preview"] == "summary for newer"
    assert "result" not in items[0]


def test_get_full_returns_the_whole_result(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    row = _seed(fake_user.user_id, query="grace", when=datetime(2026, 6, 1, tzinfo=UTC))
    row.result = {
        "summary": "Grace abounds [Romans:7].",
        "citations": [{"marker": "[Romans:7]", "book_id": str(uuid.uuid4())}],
        "degraded": [],
    }
    session = _FakeSession(history={row.history_id: row})
    _wire_session(monkeypatch, session)

    with client:
        resp = client.get(f"/search-history/{row.history_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["query"] == "grace"
    # The full result blob (citations included) is shipped for instant replay.
    assert body["result"]["summary"] == "Grace abounds [Romans:7]."
    assert body["result"]["citations"][0]["marker"] == "[Romans:7]"


def test_delete_removes_one_row(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    row = _seed(fake_user.user_id, query="grace", when=datetime(2026, 6, 1, tzinfo=UTC))
    session = _FakeSession(history={row.history_id: row})
    _wire_session(monkeypatch, session)

    with client:
        deleted = client.delete(f"/search-history/{row.history_id}")
        after = client.get(f"/search-history/{row.history_id}")

    assert deleted.status_code == 204
    assert after.status_code == 404
    assert session.history == {}


def test_clear_all_wipes_only_jwt_users_rows(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    other = _FakeUser()
    mine = _seed(fake_user.user_id, query="mine", when=datetime(2026, 6, 1, tzinfo=UTC))
    theirs = _seed(other.user_id, query="theirs", when=datetime(2026, 6, 2, tzinfo=UTC))
    session = _FakeSession(history={mine.history_id: mine, theirs.history_id: theirs})
    _wire_session(monkeypatch, session)

    with client:
        resp = client.delete("/search-history")

    assert resp.status_code == 204
    # The other tenant's row is untouched.
    assert list(session.history) == [theirs.history_id]


# --- smuggled user_id (extra="forbid" on the summary body) -------------------


def test_summary_smuggled_user_id_is_422_and_writes_no_history(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    """The only inbound body feeding the save path is ``/search-summary``; a
    smuggled ``user_id`` is a hard 422 (``extra="forbid"``), write-free."""
    monkeypatch.setattr(summary_module.settings, "llm_provider", "google")
    monkeypatch.setattr(summary_module.settings, "google_api_key", "k")
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        resp = client.post(
            "/search-summary",
            json={"query": "grace?", "user_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 422
    assert session.history == {}
    assert session.added == []


# --- no-existence-oracle 404 matrix ------------------------------------------


def test_cross_tenant_nonexistent_garbage_history_ids_are_identical_404s(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    """User B GET/DELETE on user A's row, a nonexistent id, and a non-UUID id are
    byte-identical 404s — no existence oracle, write-free."""
    user_a = _FakeUser()
    a_row = _seed(user_a.user_id, query="A's search", when=datetime(2026, 6, 1, tzinfo=UTC))
    session = _FakeSession(history={a_row.history_id: a_row})
    _wire_session(monkeypatch, session)

    with client:
        probes = [str(a_row.history_id), str(uuid.uuid4()), "not-a-uuid"]
        responses = [
            resp
            for probe in probes
            for resp in (
                client.get(f"/search-history/{probe}"),
                client.delete(f"/search-history/{probe}"),
            )
        ]

    assert all(r.status_code == 404 for r in responses)
    assert len({r.text for r in responses}) == 1  # one body across routes + probe kinds
    # Nothing committed on any 404 path — A's row untouched.
    assert session.commits == 0
    assert a_row.history_id in session.history
    assert fake_user.user_id != user_a.user_id


def test_retention_constant_is_positive() -> None:
    """The shipped cap is a sane positive constant (the named-constant convention)."""
    assert SEARCH_HISTORY_RETENTION >= 1
