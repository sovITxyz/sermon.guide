"""Unit tests for the document (sermon) routes (Phase 34 contract).

Pure-unit, no live infra. Route tests boot ``main.app`` through
``with TestClient(app):`` (lifespan runs; dev posture monkeypatched as
settings ATTRIBUTES, the suite convention) and replace auth + the DB
session via ``app.dependency_overrides`` (the ``test_reader_unit.py`` /
``test_uploads_unit.py`` pattern). The fake session resolves statements the
way the DB would — routing on the compiled SQL, predicates from the
compiled params — and keys its document store on ``document_id`` so the
soft-delete / restore / optimistic-concurrency branches behave like the
real table.

What this file pins:

- ``derive_content_text`` directly (the pure helper): text-node
  concatenation, block-level newline joins, nested marks, deep nesting,
  non-text leaf nodes, empty/malformed input;
- the create → list-preview → GET-full round trip: ``content_text`` is
  SERVER-derived (never echoed from a client value), the list ships a
  preview not the full content, ``schema_version`` is the server constant;
- optimistic concurrency: a stale ``base_updated_at`` is a 409, a fresh one
  is a 200 that bumps ``updated_at`` and re-derives ``content_text``;
- the request-model posture: a smuggled ``user_id`` AND a smuggled
  ``content_text`` are each a hard 422 (extra="forbid"); an empty PATCH is a
  422;
- the size cap: serialized content over ~2 MB is a 413 on both create and
  PATCH;
- soft-delete: a DELETE removes the doc from the list and makes GET 404
  (no oracle), restore brings it back intact, restore is idempotent on an
  active doc, a double-DELETE is a 404;
- the no-existence-oracle 404 matrix: non-owned, nonexistent, non-UUID
  garbage, AND soft-deleted ids are byte-identical 404s on GET / PATCH /
  DELETE / restore, and NOTHING past the gate runs (no write on the 404
  path);
- the OWNERSHIP matrix: user B GET/PATCH/DELETE/restore on user A's doc is a
  404 each — identical to a nonexistent id;
- every statement builder carries its ``user_id`` predicate (the
  ``test_library_unit.py`` compile-pin pattern): list filters by user_id
  AND deleted_at IS NULL, the owned-active / owned-any / delete / update
  statements all filter by user_id.
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
from documents import (
    MAX_CONTENT_BYTES,
    PREVIEW_CHARS,
    SCHEMA_VERSION,
    _delete_stmt,
    _list_stmt,
    _owned_active_stmt,
    _owned_any_stmt,
    _update_stmt,
    derive_content_text,
)
from settings import DEV_JWT_SECRET

# A representative ProseMirror/TipTap doc: a heading + a paragraph with a
# bold mark, plus a non-text leaf (hard break) that contributes nothing.
DOC_JSON: dict[str, Any] = {
    "type": "doc",
    "content": [
        {
            "type": "heading",
            "attrs": {"level": 1},
            "content": [{"type": "text", "text": "Grace"}],
        },
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "By grace "},
                {
                    "type": "text",
                    "text": "alone",
                    "marks": [{"type": "bold"}],
                },
                {"type": "hardBreak"},
            ],
        },
    ],
}
DOC_TEXT = "Grace\nBy grace \nalone"


# --- fakes -------------------------------------------------------------------


class _FakeUser:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()


class _StoredDoc:
    """A row in the fake documents table — mutated in place by the routes."""

    def __init__(
        self,
        *,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
        title: str,
        content: dict[str, Any],
        content_text: str,
        schema_version: int,
        created_at: datetime,
        updated_at: datetime,
        deleted_at: datetime | None = None,
    ) -> None:
        self.document_id = document_id
        self.user_id = user_id
        self.title = title
        self.content = content
        self.content_text = content_text
        self.schema_version = schema_version
        self.created_at = created_at
        self.updated_at = updated_at
        self.deleted_at = deleted_at


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def tuples(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return self._rows

    def one(self) -> Any:
        assert len(self._rows) == 1
        return self._rows[0]


_LIST_TUPLE_COLS = (
    "document_id",
    "title",
    "content_text",
    "schema_version",
    "created_at",
    "updated_at",
)
_UPDATE_TUPLE_COLS = (
    "document_id",
    "title",
    "content",
    "content_text",
    "schema_version",
    "created_at",
    "updated_at",
)


class _FakeSession:
    """Duck-typed AsyncSession resolving statements the way the DB would.

    Statements are routed on their compiled SQL and resolved from the
    compiled params (the ``test_reader_unit.py`` philosophy). The ``docs``
    store is keyed by ``document_id``; ``add`` stages an ORM Document whose
    attrs the route set, and ``commit``/``refresh`` mimic the insert +
    server-default read-back. ``executed`` records statement kinds in order
    so the gate-before-write ordering can be pinned and the 404 path proven
    write-free. The fake clock advances on every write so an UPDATE's
    ``updated_at`` differs from the prior value (the optimistic-concurrency
    gate).
    """

    def __init__(self, docs: dict[uuid.UUID, _StoredDoc] | None = None) -> None:
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

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: Any) -> None:
        # Mimic the INSERT read-back: assign server defaults + persist.
        if getattr(obj, "document_id", None) is None:
            obj.document_id = uuid.uuid4()
        now = self._now()
        if getattr(obj, "created_at", None) is None:
            obj.created_at = now
        if getattr(obj, "updated_at", None) is None:
            obj.updated_at = now
        if getattr(obj, "deleted_at", "missing") == "missing":
            obj.deleted_at = None
        self.docs[obj.document_id] = _StoredDoc(
            document_id=obj.document_id,
            user_id=obj.user_id,
            title=obj.title,
            content=obj.content,
            content_text=obj.content_text,
            schema_version=obj.schema_version,
            created_at=obj.created_at,
            updated_at=obj.updated_at,
            deleted_at=obj.deleted_at,
        )

    async def execute(self, stmt: Any) -> _FakeResult:
        compiled = stmt.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        params = compiled.params

        if sql.startswith("UPDATE documents SET deleted_at"):
            self.executed.append("soft_delete")
            doc = self.docs.get(params["document_id_1"])
            if doc is None or doc.user_id != params["user_id_1"] or doc.deleted_at is not None:
                return _FakeResult([])
            doc.deleted_at = params["deleted_at"]
            return _FakeResult([doc.document_id])

        if sql.startswith("UPDATE documents SET"):
            self.executed.append("update")
            doc = self.docs.get(params["document_id_1"])
            if doc is None or doc.user_id != params["user_id_1"] or doc.deleted_at is not None:
                return _FakeResult([])
            if "title" in params:
                doc.title = params["title"]
            if "content" in params:
                doc.content = params["content"]
            if "content_text" in params:
                doc.content_text = params["content_text"]
            doc.updated_at = self._now()
            return _FakeResult([self._update_row(doc)])

        if "FROM documents" in sql:
            # Distinguish the list query (no document_id predicate) from the
            # single-row gates (document_id predicate present).
            if "document_id_1" not in params:
                self.executed.append("list")
                rows = sorted(
                    (
                        d
                        for d in self.docs.values()
                        if d.user_id == params["user_id_1"] and d.deleted_at is None
                    ),
                    key=lambda d: d.updated_at,
                    reverse=True,
                )
                return _FakeResult([self._list_row(d) for d in rows])
            self.executed.append("gate")
            doc = self.docs.get(params["document_id_1"])
            if doc is None or doc.user_id != params["user_id_1"]:
                return _FakeResult([])
            # The active gate adds `deleted_at IS NULL`; the restore gate
            # (_owned_any) does not. Detect via the compiled SQL.
            active_only = "deleted_at IS NULL" in sql
            if active_only and doc.deleted_at is not None:
                return _FakeResult([])
            return _FakeResult([doc])

        msg = f"unexpected statement: {sql}"
        raise AssertionError(msg)

    @staticmethod
    def _list_row(doc: _StoredDoc) -> tuple[Any, ...]:
        return tuple(getattr(doc, col) for col in _LIST_TUPLE_COLS)

    @staticmethod
    def _update_row(doc: _StoredDoc) -> tuple[Any, ...]:
        return tuple(getattr(doc, col) for col in _UPDATE_TUPLE_COLS)


@pytest.fixture
def fake_user() -> _FakeUser:
    return _FakeUser()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, fake_user: _FakeUser) -> TestClient:
    """Dev-posture TestClient with auth overridden (test_reader_unit.py)."""
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


# --- derive_content_text (pure helper) ---------------------------------------


def test_derive_text_concatenates_and_newline_joins_blocks() -> None:
    # Block nodes (heading, paragraph) join with a newline; text nodes
    # within a block concatenate; a bold mark is transparent; a hardBreak
    # (non-text leaf) contributes nothing.
    assert derive_content_text(DOC_JSON) == DOC_TEXT


def test_derive_text_empty_doc_is_empty_string() -> None:
    assert derive_content_text({"type": "doc", "content": []}) == ""
    assert derive_content_text({"type": "doc"}) == ""


def test_derive_text_deeply_nested() -> None:
    # A bullet list → list item → paragraph → text: arbitrary depth is
    # collected, with each block boundary a newline.
    nested = {
        "type": "doc",
        "content": [
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "first"}],
                            },
                        ],
                    },
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "second"}],
                            },
                        ],
                    },
                ],
            },
        ],
    }
    assert derive_content_text(nested) == "first\nsecond"


def test_derive_text_non_text_leaf_nodes_contribute_nothing() -> None:
    doc = {
        "type": "doc",
        "content": [
            {"type": "image", "attrs": {"src": "x.png"}},
            {"type": "horizontalRule"},
        ],
    }
    assert derive_content_text(doc) == ""


def test_derive_text_text_node_without_text_key_is_safe() -> None:
    # A malformed text node (no "text") degrades to "" rather than raising.
    assert derive_content_text({"type": "text"}) == ""
    assert derive_content_text({"type": "text", "text": 123}) == ""


def test_derive_text_malformed_input_is_empty() -> None:
    assert derive_content_text(None) == ""
    assert derive_content_text("just a string") == ""
    assert derive_content_text(42) == ""


def test_derive_text_bare_list_of_nodes() -> None:
    assert derive_content_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == (
        "a\nb"
    )


# --- create → list → GET-full round trip -------------------------------------


def _create(client: TestClient, title: str = "Sermon", content: Any = None) -> dict[str, Any]:
    body = {"title": title, "content": content if content is not None else DOC_JSON}
    resp = client.post("/documents", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_derives_content_text_and_sets_schema_version(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)

    assert created["title"] == "Sermon"
    assert created["content"] == DOC_JSON
    # content_text is SERVER-derived, not echoed from any client field.
    assert created["content_text"] == DOC_TEXT
    assert created["schema_version"] == SCHEMA_VERSION
    # Persisted under the JWT user.
    stored = session.docs[uuid.UUID(created["document_id"])]
    assert stored.user_id == fake_user.user_id


def test_list_returns_preview_not_full_content(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    long_text = "x" * (PREVIEW_CHARS + 50)
    long_doc = {"type": "doc", "content": [{"type": "text", "text": long_text}]}
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        _create(client, title="Long", content=long_doc)
        resp = client.get("/documents")

    assert resp.status_code == 200
    docs = resp.json()["documents"]
    assert len(docs) == 1
    item = docs[0]
    # Preview only — capped, and the full content JSON is NOT in the list item.
    assert item["preview"] == long_text[:PREVIEW_CHARS]
    assert len(item["preview"]) == PREVIEW_CHARS
    assert "content" not in item
    assert "content_text" not in item


def test_list_excludes_other_users_and_orders_newest_first(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    other = _FakeUser()
    session = _FakeSession()
    # Seed one doc owned by another user — it must never appear.
    other_id = uuid.uuid4()
    session.docs[other_id] = _StoredDoc(
        document_id=other_id,
        user_id=other.user_id,
        title="Theirs",
        content=DOC_JSON,
        content_text=DOC_TEXT,
        schema_version=SCHEMA_VERSION,
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        updated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    _wire_session(monkeypatch, session)

    with client:
        first = _create(client, title="First")
        second = _create(client, title="Second")
        resp = client.get("/documents")

    titles = [d["title"] for d in resp.json()["documents"]]
    # Only the caller's docs; newest (Second) first.
    assert titles == ["Second", "First"]
    assert first["document_id"] != second["document_id"]


def test_get_full_returns_content(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        resp = client.get(f"/documents/{created['document_id']}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == DOC_JSON
    assert body["content_text"] == DOC_TEXT


# --- request-model posture: extra="forbid" + size cap ------------------------


def test_create_smuggled_user_id_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        resp = client.post(
            "/documents",
            json={"title": "X", "content": DOC_JSON, "user_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 422
    assert session.added == []  # nothing persisted


def test_create_smuggled_content_text_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    # content_text is server-derived; a client value must fail loud, not be
    # silently dropped (it could disagree with content).
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        resp = client.post(
            "/documents",
            json={"title": "X", "content": DOC_JSON, "content_text": "fake preview"},
        )

    assert resp.status_code == 422
    assert session.added == []


def test_create_over_2mb_is_413(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    huge = "z" * (MAX_CONTENT_BYTES + 1)
    huge_doc = {"type": "doc", "content": [{"type": "text", "text": huge}]}
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        resp = client.post("/documents", json={"title": "Big", "content": huge_doc})

    assert resp.status_code == 413
    assert session.added == []  # rejected before any persist


# --- PATCH: optimistic concurrency, partial, re-derive -----------------------


def test_patch_fresh_base_updates_and_rederives(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        new_doc = {"type": "doc", "content": [{"type": "text", "text": "Mercy"}]}
        resp = client.patch(
            f"/documents/{created['document_id']}",
            json={"base_updated_at": created["updated_at"], "content": new_doc},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content"] == new_doc
    # content_text re-derived from the new content.
    assert body["content_text"] == "Mercy"
    # updated_at bumped past the create value (the gate moved).
    assert body["updated_at"] != created["updated_at"]


def test_patch_title_only_leaves_content(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        resp = client.patch(
            f"/documents/{created['document_id']}",
            json={"base_updated_at": created["updated_at"], "title": "Renamed"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Renamed"
    assert body["content"] == DOC_JSON  # unchanged
    assert body["content_text"] == DOC_TEXT  # unchanged


def test_patch_stale_base_is_409(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        stale = (datetime.fromisoformat(created["updated_at"]) - timedelta(hours=1)).isoformat()
        resp = client.patch(
            f"/documents/{created['document_id']}",
            json={"base_updated_at": stale, "title": "Nope"},
        )

    assert resp.status_code == 409
    # The doc was NOT mutated by the rejected PATCH.
    stored = session.docs[uuid.UUID(created["document_id"])]
    assert stored.title == "Sermon"


def test_patch_second_write_with_first_base_is_409(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    """A real concurrent-edit scenario: two PATCHes off the same base.

    The first wins (200, bumps updated_at); the second carries the now-stale
    original base and 409s.
    """
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        base = created["updated_at"]
        first = client.patch(
            f"/documents/{created['document_id']}",
            json={"base_updated_at": base, "title": "A"},
        )
        second = client.patch(
            f"/documents/{created['document_id']}",
            json={"base_updated_at": base, "title": "B"},
        )

    assert first.status_code == 200
    assert second.status_code == 409


def test_patch_smuggled_user_id_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        resp = client.patch(
            f"/documents/{created['document_id']}",
            json={
                "base_updated_at": created["updated_at"],
                "title": "X",
                "user_id": str(uuid.uuid4()),
            },
        )

    assert resp.status_code == 422


def test_patch_smuggled_content_text_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        resp = client.patch(
            f"/documents/{created['document_id']}",
            json={
                "base_updated_at": created["updated_at"],
                "content_text": "smuggled",
            },
        )

    assert resp.status_code == 422


def test_patch_empty_body_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    # base_updated_at present but neither title nor content — nothing to do.
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        resp = client.patch(
            f"/documents/{created['document_id']}",
            json={"base_updated_at": created["updated_at"]},
        )

    assert resp.status_code == 422


def test_patch_missing_base_updated_at_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        resp = client.patch(f"/documents/{created['document_id']}", json={"title": "X"})

    assert resp.status_code == 422


def test_patch_over_2mb_content_is_413(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        huge = "z" * (MAX_CONTENT_BYTES + 1)
        huge_doc = {"type": "doc", "content": [{"type": "text", "text": huge}]}
        resp = client.patch(
            f"/documents/{created['document_id']}",
            json={"base_updated_at": created["updated_at"], "content": huge_doc},
        )

    assert resp.status_code == 413


# --- soft delete / restore ----------------------------------------------------


def test_delete_vanishes_from_list_and_get_404_then_restore(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        doc_id = created["document_id"]

        delete = client.delete(f"/documents/{doc_id}")
        assert delete.status_code == 204

        # Vanished from the list and GET 404s (no oracle).
        assert client.get("/documents").json()["documents"] == []
        assert client.get(f"/documents/{doc_id}").status_code == 404

        # Restore brings it back intact.
        restore = client.post(f"/documents/{doc_id}/restore")
        assert restore.status_code == 200
        restored = restore.json()
        assert restored["content"] == DOC_JSON
        assert restored["content_text"] == DOC_TEXT
        assert restored["title"] == "Sermon"

        # Back in the list and GET-able again.
        assert len(client.get("/documents").json()["documents"]) == 1
        assert client.get(f"/documents/{doc_id}").status_code == 200


def test_double_delete_is_404(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        doc_id = created["document_id"]
        assert client.delete(f"/documents/{doc_id}").status_code == 204
        # Second DELETE: already soft-deleted → 404 (active-row predicate
        # matches nothing).
        assert client.delete(f"/documents/{doc_id}").status_code == 404


def test_restore_idempotent_on_active_doc(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        doc_id = created["document_id"]
        # Restoring an already-active doc is a no-op 200 (idempotent).
        resp = client.post(f"/documents/{doc_id}/restore")

    assert resp.status_code == 200
    assert resp.json()["document_id"] == doc_id


def test_patch_on_soft_deleted_is_404(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        doc_id = created["document_id"]
        client.delete(f"/documents/{doc_id}")
        resp = client.patch(
            f"/documents/{doc_id}",
            json={"base_updated_at": created["updated_at"], "title": "X"},
        )

    assert resp.status_code == 404


# --- the no-existence-oracle 404 matrix ---------------------------------------


def test_unknown_garbage_and_soft_deleted_are_identical_404s(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    """Nonexistent, non-UUID garbage, and soft-deleted ids: identical 404s.

    No body differs across the probe kinds OR the routes — no existence
    oracle. And nothing past the gate runs on the 404 path.

    GET / PATCH / DELETE treat a soft-deleted doc as 404 (the active gate);
    restore is deliberately EXCLUDED for the soft-deleted id because restore
    is its legitimate inverse (it would 200) — restore's own 404 matrix is
    nonexistent + garbage only.
    """
    session = _FakeSession()
    _wire_session(monkeypatch, session)

    with client:
        created = _create(client)
        soft_deleted = created["document_id"]
        client.delete(f"/documents/{soft_deleted}")  # now soft-deleted

        # Reset the execution + commit log so we can prove the 404 path is
        # write-free (the create + initial delete above each committed once).
        session.executed.clear()
        session.commits = 0
        active_gate_probes = [str(uuid.uuid4()), "not-a-uuid", soft_deleted]
        restore_probes = [str(uuid.uuid4()), "not-a-uuid"]
        responses = [
            resp
            for probe in active_gate_probes
            for resp in (
                client.get(f"/documents/{probe}"),
                client.patch(
                    f"/documents/{probe}",
                    json={"base_updated_at": "2026-06-15T12:00:01+00:00", "title": "x"},
                ),
                client.delete(f"/documents/{probe}"),
            )
        ]
        responses += [client.post(f"/documents/{probe}/restore") for probe in restore_probes]

    assert all(r.status_code == 404 for r in responses)
    assert len({r.text for r in responses}) == 1  # one body across routes + probe kinds
    # No update/soft_delete write ran on any 404 path; the soft-deleted doc
    # stays deleted.
    assert "update" not in session.executed
    assert session.commits == 0
    assert session.docs[uuid.UUID(soft_deleted)].deleted_at is not None


# --- ownership matrix (user B on user A's doc) --------------------------------


def test_user_b_cannot_touch_user_a_doc(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    """User B GET/PATCH/DELETE/restore on user A's doc → 404 each.

    Same 404 as a nonexistent id — no 403, no existence oracle (the
    api/AGENTS.md cross-tenant-404 rule). The doc IS owned by A, exactly the
    case a different status would leak.
    """
    user_a = _FakeUser()
    a_doc_id = uuid.uuid4()
    session = _FakeSession(
        docs={
            a_doc_id: _StoredDoc(
                document_id=a_doc_id,
                user_id=user_a.user_id,
                title="A's sermon",
                content=DOC_JSON,
                content_text=DOC_TEXT,
                schema_version=SCHEMA_VERSION,
                created_at=datetime(2026, 6, 1, tzinfo=UTC),
                updated_at=datetime(2026, 6, 1, tzinfo=UTC),
            ),
        },
    )
    _wire_session(monkeypatch, session)

    # The fixture's `fake_user` is user B; A's doc is not B's.
    with client:
        get = client.get(f"/documents/{a_doc_id}")
        patch = client.patch(
            f"/documents/{a_doc_id}",
            json={"base_updated_at": "2026-06-01T00:00:00+00:00", "title": "hijack"},
        )
        delete = client.delete(f"/documents/{a_doc_id}")
        restore = client.post(f"/documents/{a_doc_id}/restore")
        nonexistent = client.get(f"/documents/{uuid.uuid4()}")

    for resp in (get, patch, delete, restore):
        assert resp.status_code == 404
    # Cross-tenant 404 body is identical to a nonexistent-id 404.
    assert get.json() == nonexistent.json()
    # A's doc is untouched — no mutation, no soft-delete.
    assert session.docs[a_doc_id].title == "A's sermon"
    assert session.docs[a_doc_id].deleted_at is None
    assert fake_user.user_id != user_a.user_id


# --- statement compile pins (tenant audit) ------------------------------------


def test_list_stmt_filters_by_user_and_excludes_deleted() -> None:
    user_id = uuid.uuid4()
    compiled = _list_stmt(user_id).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    # The load-bearing tenant filter + the soft-delete exclusion + ordering.
    assert "documents.user_id =" in sql
    assert "documents.deleted_at IS NULL" in sql
    assert "ORDER BY documents.updated_at DESC" in sql
    assert user_id in compiled.params.values()


def test_owned_active_stmt_is_user_and_active_scoped() -> None:
    document_id, user_id = uuid.uuid4(), uuid.uuid4()
    compiled = _owned_active_stmt(document_id, user_id).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "documents.document_id =" in sql
    assert "documents.user_id =" in sql
    assert "documents.deleted_at IS NULL" in sql
    assert set(compiled.params.values()) == {document_id, user_id}


def test_owned_any_stmt_is_user_scoped_without_active_predicate() -> None:
    document_id, user_id = uuid.uuid4(), uuid.uuid4()
    compiled = _owned_any_stmt(document_id, user_id).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    # Restore must see soft-deleted rows — no deleted_at predicate — but the
    # user_id gate is STILL load-bearing (a cross-tenant restore is a 404).
    assert "documents.document_id =" in sql
    assert "documents.user_id =" in sql
    assert "deleted_at IS NULL" not in sql
    assert set(compiled.params.values()) == {document_id, user_id}


def test_delete_stmt_is_user_and_active_scoped() -> None:
    document_id, user_id = uuid.uuid4(), uuid.uuid4()
    now = datetime(2026, 6, 15, tzinfo=UTC)
    compiled = _delete_stmt(document_id, user_id, now=now).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith("UPDATE documents SET deleted_at")
    assert "documents.document_id =" in sql
    assert "documents.user_id =" in sql
    assert "documents.deleted_at IS NULL" in sql
    assert "RETURNING" in sql
    assert document_id in compiled.params.values()
    assert user_id in compiled.params.values()


def test_update_stmt_is_user_scoped_and_bumps_updated_at() -> None:
    document_id, user_id = uuid.uuid4(), uuid.uuid4()
    compiled = _update_stmt(
        document_id,
        user_id,
        values={"title": "X"},
    ).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith("UPDATE documents SET")
    assert "documents.document_id =" in sql
    assert "documents.user_id =" in sql
    assert "documents.deleted_at IS NULL" in sql
    # updated_at bumped explicitly via now() (no onupdate on the column).
    assert "updated_at=now()" in sql
    assert "RETURNING" in sql
    assert document_id in compiled.params.values()
    assert user_id in compiled.params.values()
