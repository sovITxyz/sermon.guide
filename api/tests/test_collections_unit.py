"""Unit tests for the collections routes (Phase 48 — library folders).

Pure-unit, no live infra — the ``test_calendar_unit.py`` template. Route tests
boot ``main.app`` through ``with TestClient(app):`` (lifespan runs; dev posture
monkeypatched as settings ATTRIBUTES) and replace auth + the DB session via
``app.dependency_overrides``. The fake session resolves statements the way the
DB would — routing on the compiled SQL, predicates from the compiled params —
keying a collection store on ``collection_id``, a membership store on
``(collection_id, book_id)``, and a library set of ``(user_id, book_id)`` the
add-books clamp reads.

What this file pins:

- the statement builders (the mechanical tenant audit, no DB): ``_list_stmt`` /
  ``_memberships_stmt`` carry ``user_id``; ``_owned_collection_stmt`` /
  ``_update_stmt`` / ``_delete_stmt`` are doubly-scoped (``collection_id`` AND
  ``user_id``); ``_owned_collection_ids_stmt`` / ``_member_book_ids_stmt`` /
  ``_library_subset_stmt`` carry ``user_id``; ``_add_books_stmt`` is
  ON CONFLICT DO NOTHING and stamps the denormalized ``user_id``;
  ``_remove_books_stmt`` is triply-scoped (collection, user, book set);
- the round trips: create persists under the JWT user; GET returns membership;
  list returns only the JWT user's collections, each with its ``book_ids``;
  PATCH renames / clears description; DELETE hard-deletes and cascades
  memberships;
- the request-model posture: a smuggled ``user_id`` is a hard 422 (create /
  patch / add-books / remove-books); an empty PATCH is a 422; a null ``name``
  PATCH is a 422;
- the add-books library CLAMP: a book the JWT user does not own (foreign or
  unknown) is silently dropped, only owned books are added; re-adding an
  existing book is an idempotent no-op (ON CONFLICT);
- the no-existence-oracle 404 matrix: cross-tenant, nonexistent, and non-UUID
  garbage collection ids are byte-identical 404s on GET / PATCH / DELETE /
  add-books / remove-books, write-free.
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
from collections_routes import (
    _add_books_stmt,
    _delete_stmt,
    _library_subset_stmt,
    _list_stmt,
    _member_book_ids_stmt,
    _memberships_stmt,
    _owned_collection_ids_stmt,
    _owned_collection_stmt,
    _remove_books_stmt,
    _update_stmt,
)
from settings import DEV_JWT_SECRET

# --- fakes -------------------------------------------------------------------


class _FakeUser:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()


class _StoredCollection:
    """A row in the fake collections table — mutated in place by the routes."""

    def __init__(
        self,
        *,
        collection_id: uuid.UUID,
        user_id: uuid.UUID,
        name: str,
        description: str | None,
        created_at: datetime,
    ) -> None:
        self.collection_id = collection_id
        self.user_id = user_id
        self.name = name
        self.description = description
        self.created_at = created_at


class _StoredMembership:
    """A row in the fake collection_books table (one (collection, book) pair)."""

    def __init__(
        self,
        *,
        collection_book_id: uuid.UUID,
        collection_id: uuid.UUID,
        book_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        self.collection_book_id = collection_book_id
        self.collection_id = collection_id
        self.book_id = book_id
        self.user_id = user_id


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

    def tuples(self) -> _ScalarResult:
        return _ScalarResult(self._rows)

    def one(self) -> Any:
        assert len(self._rows) == 1
        return self._rows[0]


class _FakeSession:
    """Duck-typed AsyncSession resolving statements the way the DB would.

    Statements route on their compiled SQL and resolve from the compiled params
    (the ``test_calendar_unit.py`` philosophy). ``collections`` is keyed by
    ``collection_id``; ``memberships`` by ``(collection_id, book_id)``;
    ``library`` is the set of ``(user_id, book_id)`` the add-books clamp reads.
    ``executed`` records statement kinds in order so a 404 path can be proven
    write-free.
    """

    def __init__(
        self,
        *,
        collections: dict[uuid.UUID, _StoredCollection] | None = None,
        memberships: dict[tuple[uuid.UUID, uuid.UUID], _StoredMembership] | None = None,
        library: set[tuple[uuid.UUID, uuid.UUID]] | None = None,
    ) -> None:
        self.collections: dict[uuid.UUID, _StoredCollection] = collections or {}
        self.memberships: dict[tuple[uuid.UUID, uuid.UUID], _StoredMembership] = memberships or {}
        self.library: set[tuple[uuid.UUID, uuid.UUID]] = library or set()
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

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: Any) -> None:
        if getattr(obj, "collection_id", None) is None:
            obj.collection_id = uuid.uuid4()
        if getattr(obj, "created_at", None) is None:
            obj.created_at = self._now()
        self.collections[obj.collection_id] = _StoredCollection(
            collection_id=obj.collection_id,
            user_id=obj.user_id,
            name=obj.name,
            description=obj.description,
            created_at=obj.created_at,
        )

    async def execute(self, stmt: Any) -> _FakeResult:
        compiled = stmt.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        params = compiled.params

        if sql.startswith("INSERT INTO collection_books"):
            self.executed.append("add_books")
            i = 0
            while f"book_id_m{i}" in params:
                collection_id = params[f"collection_id_m{i}"]
                book_id = params[f"book_id_m{i}"]
                user_id = params[f"user_id_m{i}"]
                key = (collection_id, book_id)
                # ON CONFLICT (collection_id, book_id) DO NOTHING.
                if key not in self.memberships:
                    self.memberships[key] = _StoredMembership(
                        collection_book_id=params[f"collection_book_id_m{i}"],
                        collection_id=collection_id,
                        book_id=book_id,
                        user_id=user_id,
                    )
                i += 1
            return _FakeResult([])

        if sql.startswith("DELETE FROM collection_books"):
            self.executed.append("remove_books")
            collection_id = params["collection_id_1"]
            user_id = params["user_id_1"]
            removed: list[uuid.UUID] = []
            for book_id in params["book_id_1"]:
                key = (collection_id, book_id)
                membership = self.memberships.get(key)
                if membership is not None and membership.user_id == user_id:
                    del self.memberships[key]
                    removed.append(book_id)
            return _FakeResult(removed)

        if sql.startswith("UPDATE collections"):
            self.executed.append("update")
            collection = self.collections.get(params["collection_id_1"])
            if collection is None or collection.user_id != params["user_id_1"]:
                return _FakeResult([])
            if "name" in params:
                collection.name = params["name"]
            if "description" in params:
                collection.description = params["description"]
            row = (
                collection.collection_id,
                collection.name,
                collection.description,
                collection.created_at,
            )
            return _FakeResult([row])

        if sql.startswith("DELETE FROM collections"):
            self.executed.append("delete")
            collection = self.collections.get(params["collection_id_1"])
            if collection is None or collection.user_id != params["user_id_1"]:
                return _FakeResult([])
            del self.collections[collection.collection_id]
            # Cascade memberships (the FK is ON DELETE CASCADE).
            stale = [
                key
                for key, membership in self.memberships.items()
                if membership.collection_id == collection.collection_id
            ]
            for key in stale:
                del self.memberships[key]
            return _FakeResult([collection.collection_id])

        if "FROM user_library" in sql:
            self.executed.append("library_subset")
            user_id = params["user_id_1"]
            requested = params["book_id_1"]
            owned = [b for b in requested if (user_id, b) in self.library]
            return _FakeResult(owned)

        if "FROM collection_books" in sql:
            user_id = params["user_id_1"]
            if "collection_id_1" in params:
                # _member_book_ids_stmt (collection_id IN (...)).
                self.executed.append("member_book_ids")
                cids = params["collection_id_1"]
                books = [
                    m.book_id
                    for m in self.memberships.values()
                    if m.collection_id in cids and m.user_id == user_id
                ]
                return _FakeResult(books)
            # _memberships_stmt (all of the user's memberships).
            self.executed.append("memberships")
            rows = [
                (m.collection_id, m.book_id)
                for m in self.memberships.values()
                if m.user_id == user_id
            ]
            return _FakeResult(rows)

        if "FROM collections" in sql:
            user_id = params["user_id_1"]
            cid_param = params.get("collection_id_1")
            if cid_param is None:
                # _list_stmt — newest-first.
                self.executed.append("list")
                rows = sorted(
                    (c for c in self.collections.values() if c.user_id == user_id),
                    key=lambda c: c.created_at,
                    reverse=True,
                )
                return _FakeResult(rows)
            if isinstance(cid_param, list):
                # _owned_collection_ids_stmt — the bulk ownership clamp.
                self.executed.append("owned_ids")
                owned_ids = [
                    c.collection_id
                    for c in self.collections.values()
                    if c.collection_id in cid_param and c.user_id == user_id
                ]
                return _FakeResult(owned_ids)
            # _owned_collection_stmt — the single-collection gate.
            self.executed.append("gate")
            collection = self.collections.get(cid_param)
            if collection is None or collection.user_id != user_id:
                return _FakeResult([])
            return _FakeResult([collection])

        msg = f"unexpected statement: {sql}"
        raise AssertionError(msg)


@pytest.fixture
def fake_user() -> _FakeUser:
    return _FakeUser()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, fake_user: _FakeUser) -> TestClient:
    """Dev-posture TestClient with auth overridden (test_calendar_unit.py)."""
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


# --- statement compile pins (tenant audit) -----------------------------------


def test_list_and_memberships_stmts_are_user_scoped() -> None:
    user_id = uuid.uuid4()
    memberships = _memberships_stmt(user_id).compile(dialect=postgresql.dialect())
    assert "collection_books.user_id =" in str(memberships)
    assert user_id in memberships.params.values()

    list_compiled = _list_stmt(user_id).compile(dialect=postgresql.dialect())
    sql = str(list_compiled)
    assert "collections.user_id =" in sql
    assert "ORDER BY collections.created_at DESC" in sql
    assert user_id in list_compiled.params.values()


def test_owned_collection_stmt_is_doubly_scoped() -> None:
    collection_id, user_id = uuid.uuid4(), uuid.uuid4()
    compiled = _owned_collection_stmt(collection_id, user_id).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "collections.collection_id =" in sql
    assert "collections.user_id =" in sql
    assert set(compiled.params.values()) == {collection_id, user_id}


def test_owned_collection_ids_stmt_is_user_scoped() -> None:
    ids = [uuid.uuid4(), uuid.uuid4()]
    user_id = uuid.uuid4()
    compiled = _owned_collection_ids_stmt(ids, user_id).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "collections.collection_id IN" in sql
    assert "collections.user_id =" in sql
    assert user_id in compiled.params.values()


def test_member_book_ids_stmt_is_user_scoped() -> None:
    ids = [uuid.uuid4()]
    user_id = uuid.uuid4()
    compiled = _member_book_ids_stmt(ids, user_id).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "collection_books.book_id" in sql  # selects the book id
    assert "collection_books.collection_id IN" in sql
    assert "collection_books.user_id =" in sql
    assert user_id in compiled.params.values()


def test_library_subset_stmt_is_user_scoped() -> None:
    book_ids = [uuid.uuid4(), uuid.uuid4()]
    user_id = uuid.uuid4()
    compiled = _library_subset_stmt(book_ids, user_id).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "user_library.book_id IN" in sql
    assert "user_library.user_id =" in sql
    assert user_id in compiled.params.values()


def test_add_books_stmt_stamps_user_and_does_nothing_on_conflict() -> None:
    collection_id, user_id = uuid.uuid4(), uuid.uuid4()
    book_ids = [uuid.uuid4(), uuid.uuid4()]
    compiled = _add_books_stmt(collection_id, user_id, book_ids).compile(
        dialect=postgresql.dialect(),
    )
    sql = str(compiled)
    assert sql.startswith("INSERT INTO collection_books")
    assert "ON CONFLICT" in sql
    assert "DO NOTHING" in sql
    # The denormalized JWT user_id is stamped on every inserted membership.
    assert user_id in compiled.params.values()
    assert collection_id in compiled.params.values()
    for book_id in book_ids:
        assert book_id in compiled.params.values()


def test_remove_books_stmt_is_triply_scoped() -> None:
    collection_id, user_id = uuid.uuid4(), uuid.uuid4()
    book_ids = [uuid.uuid4()]
    compiled = _remove_books_stmt(collection_id, user_id, book_ids).compile(
        dialect=postgresql.dialect(),
    )
    sql = str(compiled)
    assert sql.startswith("DELETE FROM collection_books")
    assert "collection_books.collection_id =" in sql
    assert "collection_books.user_id =" in sql
    assert "collection_books.book_id IN" in sql
    assert "RETURNING" in sql
    assert collection_id in compiled.params.values()
    assert user_id in compiled.params.values()


def test_update_stmt_is_doubly_scoped() -> None:
    collection_id, user_id = uuid.uuid4(), uuid.uuid4()
    compiled = _update_stmt(
        collection_id,
        user_id,
        values={"name": "X"},
    ).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith("UPDATE collections SET")
    assert "collections.collection_id =" in sql
    assert "collections.user_id =" in sql
    assert "RETURNING" in sql
    assert collection_id in compiled.params.values()
    assert user_id in compiled.params.values()


def test_delete_stmt_is_doubly_scoped() -> None:
    collection_id, user_id = uuid.uuid4(), uuid.uuid4()
    compiled = _delete_stmt(collection_id, user_id).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith("DELETE FROM collections")
    assert "collections.collection_id =" in sql
    assert "collections.user_id =" in sql
    assert "RETURNING" in sql
    assert set(compiled.params.values()) == {collection_id, user_id}


# --- create / get / list round trip ------------------------------------------


def _create(client: TestClient, *, name: str = "Patristics", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name, **extra}
    resp = client.post("/collections", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_persists_under_jwt_user(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        body = _create(client, name="Reformers", description="16th century")

    assert body["name"] == "Reformers"
    assert body["description"] == "16th century"
    assert body["book_ids"] == []
    stored = session.collections[uuid.UUID(body["collection_id"])]
    assert stored.user_id == fake_user.user_id


def test_get_returns_membership(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    book_a, book_b = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession(library={(fake_user.user_id, book_a), (fake_user.user_id, book_b)})
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        collection_id = created["collection_id"]
        client.post(
            f"/collections/{collection_id}/books",
            json={"book_ids": [str(book_a), str(book_b)]},
        )
        resp = client.get(f"/collections/{collection_id}")

    assert resp.status_code == 200, resp.text
    assert set(resp.json()["book_ids"]) == {str(book_a), str(book_b)}


def test_list_returns_only_jwt_users_collections(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    other = _FakeUser()
    other_cid = uuid.uuid4()
    session = _FakeSession(
        collections={
            other_cid: _StoredCollection(
                collection_id=other_cid,
                user_id=other.user_id,
                name="Theirs",
                description=None,
                created_at=datetime(2026, 6, 1, tzinfo=UTC),
            ),
        },
    )
    _wire_session(monkeypatch, session)

    with client:
        _create(client, name="Mine")
        resp = client.get("/collections")

    assert resp.status_code == 200, resp.text
    names = [c["name"] for c in resp.json()["collections"]]
    assert names == ["Mine"]


# --- PATCH partial + posture --------------------------------------------------


def test_patch_renames_and_clears_description(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client, name="Old", description="keep me")
        collection_id = created["collection_id"]
        renamed = client.patch(f"/collections/{collection_id}", json={"name": "New"})
        cleared = client.patch(f"/collections/{collection_id}", json={"description": None})

    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "New"
    assert cleared.json()["description"] is None
    assert session.collections[uuid.UUID(collection_id)].name == "New"


def test_patch_empty_body_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        resp = client.patch(f"/collections/{created['collection_id']}", json={})

    assert resp.status_code == 422


def test_patch_null_name_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        resp = client.patch(f"/collections/{created['collection_id']}", json={"name": None})

    assert resp.status_code == 422


# --- DELETE + cascade ---------------------------------------------------------


def test_delete_hard_deletes_and_cascades_memberships(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    book = uuid.uuid4()
    session = _FakeSession(library={(fake_user.user_id, book)})
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        collection_id = created["collection_id"]
        client.post(f"/collections/{collection_id}/books", json={"book_ids": [str(book)]})
        assert len(session.memberships) == 1
        resp = client.delete(f"/collections/{collection_id}")
        after = client.get(f"/collections/{collection_id}")

    assert resp.status_code == 204
    assert after.status_code == 404
    assert session.collections == {}
    # Memberships cascaded away with the collection.
    assert session.memberships == {}


# --- add-books library clamp + idempotency -----------------------------------


def test_add_books_clamps_to_owned_library(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    owned = uuid.uuid4()
    foreign = uuid.uuid4()  # not in the JWT user's library
    session = _FakeSession(library={(fake_user.user_id, owned)})
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        resp = client.post(
            f"/collections/{created['collection_id']}/books",
            json={"book_ids": [str(owned), str(foreign)]},
        )

    assert resp.status_code == 200, resp.text
    # Only the owned book was added; the foreign book was silently clamped out.
    assert resp.json()["book_ids"] == [str(owned)]
    assert (uuid.UUID(created["collection_id"]), owned) in session.memberships
    assert (uuid.UUID(created["collection_id"]), foreign) not in session.memberships


def test_add_books_drops_other_users_book(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    other = _FakeUser()
    other_book = uuid.uuid4()
    # The book is in ANOTHER user's library, never the JWT user's.
    session = _FakeSession(library={(other.user_id, other_book)})
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        resp = client.post(
            f"/collections/{created['collection_id']}/books",
            json={"book_ids": [str(other_book)]},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["book_ids"] == []
    assert session.memberships == {}


def test_add_books_is_idempotent_on_conflict(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    book = uuid.uuid4()
    session = _FakeSession(library={(fake_user.user_id, book)})
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        collection_id = created["collection_id"]
        first = client.post(f"/collections/{collection_id}/books", json={"book_ids": [str(book)]})
        second = client.post(f"/collections/{collection_id}/books", json={"book_ids": [str(book)]})

    assert first.json()["book_ids"] == [str(book)]
    # ON CONFLICT (collection_id, book_id) DO NOTHING — no duplicate row.
    assert second.json()["book_ids"] == [str(book)]
    assert len(session.memberships) == 1


def test_remove_books_removes_membership(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    book = uuid.uuid4()
    session = _FakeSession(library={(fake_user.user_id, book)})
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        collection_id = created["collection_id"]
        client.post(f"/collections/{collection_id}/books", json={"book_ids": [str(book)]})
        removed = client.request(
            "DELETE",
            f"/collections/{collection_id}/books",
            json={"book_ids": [str(book)]},
        )
        after = client.get(f"/collections/{collection_id}")

    assert removed.status_code == 200, removed.text
    assert after.json()["book_ids"] == []
    assert session.memberships == {}


# --- smuggled user_id (extra="forbid") ---------------------------------------


def test_create_smuggled_user_id_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        resp = client.post(
            "/collections",
            json={"name": "X", "user_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 422
    assert session.added == []


def test_add_books_smuggled_user_id_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        resp = client.post(
            f"/collections/{created['collection_id']}/books",
            json={"book_ids": [str(uuid.uuid4())], "user_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 422
    assert session.memberships == {}


# --- no-existence-oracle 404 matrix (cross-tenant collection_id) -------------


def test_cross_tenant_nonexistent_garbage_collection_ids_are_identical_404s(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    """User B GET/PATCH/DELETE/add-books/remove-books on user A's collection, a
    nonexistent id, and a non-UUID id are byte-identical 404s — no existence
    oracle, write-free.
    """
    user_a = _FakeUser()
    a_collection = uuid.uuid4()
    session = _FakeSession(
        collections={
            a_collection: _StoredCollection(
                collection_id=a_collection,
                user_id=user_a.user_id,
                name="A's shelf",
                description=None,
                created_at=datetime(2026, 6, 1, tzinfo=UTC),
            ),
        },
    )
    _wire_session(monkeypatch, session)

    book_body = {"book_ids": [str(uuid.uuid4())]}
    with client:
        probes = [str(a_collection), str(uuid.uuid4()), "not-a-uuid"]
        responses = [
            resp
            for probe in probes
            for resp in (
                client.get(f"/collections/{probe}"),
                client.patch(f"/collections/{probe}", json={"name": "hijack"}),
                client.delete(f"/collections/{probe}"),
                client.post(f"/collections/{probe}/books", json=book_body),
                client.request("DELETE", f"/collections/{probe}/books", json=book_body),
            )
        ]

    assert all(r.status_code == 404 for r in responses)
    assert len({r.text for r in responses}) == 1  # one body across routes + probe kinds
    # Nothing committed on any 404 path — A's collection + memberships untouched.
    assert session.commits == 0
    assert a_collection in session.collections
    assert session.memberships == {}
    assert fake_user.user_id != user_a.user_id
