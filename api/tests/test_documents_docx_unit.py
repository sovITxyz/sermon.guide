"""Unit tests for the DOCX round-trip routes (Phase 43 contract).

`GET /documents/{document_id}/export.docx` + `POST
/documents/{document_id}/import` add a Word round-trip on top of the
canonical TipTap/ProseMirror JSON in `documents.content`. This suite pins
the API-layer contract WITHOUT a live Postgres, mirroring
`test_documents_unit.py`: route tests boot `main.app` through `with
TestClient(app):` (lifespan runs; dev posture monkeypatched as settings
ATTRIBUTES), override auth + the DB session via `app.dependency_overrides`,
and use a fake session that resolves statements by their compiled SQL +
params. `worker.convert` is monkeypatched at the `documents` module seam so
no real pandoc/Node round-trip fires for the contract tests; ONE end-to-end
test exercises the real convert path, skip-guarded when pandoc/Node/the
`convert_node` bundle are absent.

What this file pins:

- **export**: the owned doc streams as `.docx` with the docx Content-Type
  and a sanitized `Content-Disposition` filename; a non-owned / nonexistent
  / non-UUID id is the same 404 (no oracle) and `convert_to_docx` is NEVER
  called past the gate; a `ConversionError` is a fixed-detail 502;
- **import — snapshot FIRST**: a `sermon_doc_revisions` row holding the
  PRIOR content/content_text/user_id is inserted BEFORE the documents
  UPDATE (the recorded statement order is `gate, insert_revision, update`),
  the snapshot's `user_id` is the JWT user, and `content_text` is
  RE-DERIVED (never the conversion output);
- **import — edge defenses**: a non-docx upload is a 415 and a >cap upload
  is a 413, BOTH before the gate / convert / any snapshot; the staged /tmp
  file is ALWAYS cleaned (success and failure);
- **import — 404 matrix**: non-owned / nonexistent / non-UUID ids are
  byte-identical 404s and NOTHING past the gate runs (no snapshot, no
  update);
- **import — convert failure** is a fixed-detail 502 with NO snapshot/update
  committed;
- the `_revision_insert_stmt` tenant seam: the snapshot carries the
  JWT-derived `user_id` (compile-pinned, the `_xxx_stmt` convention);
- the multipart contract: a missing `file` part is a 422 (FastAPI's
  `File(...)` requirement — the import route takes no JSON body to smuggle
  fields through).
"""

# Tests exercise module-internals and pass duck-typed fakes on purpose.
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

import auth
import documents as documents_module
import main as main_module
from documents import (
    MAX_CONTENT_BYTES,
    SCHEMA_VERSION,
    _export_filename,
    _revision_insert_stmt,
    derive_content_text,
)
from settings import DEV_JWT_SECRET

_WORKER_ROOT = Path(__file__).resolve().parent.parent.parent / "worker"
_NODE_CLI = _WORKER_ROOT / "convert_node" / "cli.mjs"
_NODE_MODULES = _WORKER_ROOT / "convert_node" / "node_modules"
_REFERENCE_DOCX = _WORKER_ROOT / "assets" / "reference.docx"

# The wire MIME a real .docx (OOXML zip) sniffs as / the export streams with.
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# A representative prior document (before an import overwrites it): a heading
# + a bold paragraph. content_text is the server-derived projection.
PRIOR_DOC: dict[str, Any] = {
    "type": "doc",
    "content": [
        {
            "type": "heading",
            "attrs": {"level": 1},
            "content": [{"type": "text", "text": "Grace"}],
        },
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": "By grace alone"}],
        },
    ],
}
PRIOR_TEXT = "Grace\nBy grace alone"

# What the (mocked) convert_from_docx returns — the NEW content an import
# overwrites the prior doc with.
IMPORTED_DOC: dict[str, Any] = {
    "type": "doc",
    "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Reworked in Word"}]},
    ],
}
IMPORTED_TEXT = "Reworked in Word"


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
        scope_book_ids: list[str] | None = None,
        scope_collection_ids: list[str] | None = None,
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
        # Phase 50 scope columns ride through the _update_stmt RETURNING; the
        # docx import / pull paths never change them, so they default to [].
        self.scope_book_ids: list[str] = scope_book_ids if scope_book_ids is not None else []
        self.scope_collection_ids: list[str] = (
            scope_collection_ids if scope_collection_ids is not None else []
        )


class _Revision:
    """A captured sermon_doc_revisions INSERT — the snapshot row."""

    def __init__(self, params: dict[str, Any]) -> None:
        self.document_id = params["document_id"]
        self.user_id = params["user_id"]
        self.content = params["content"]
        self.content_text = params["content_text"]
        self.schema_version = params["schema_version"]
        self.source = params["source"]


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def one(self) -> Any:
        assert len(self._rows) == 1
        return self._rows[0]


_UPDATE_TUPLE_COLS = (
    "document_id",
    "title",
    "content",
    "content_text",
    "schema_version",
    "scope_book_ids",
    "scope_collection_ids",
    "created_at",
    "updated_at",
)


class _FakeSession:
    """Duck-typed AsyncSession resolving statements the way the DB would.

    Statements route on their compiled SQL; predicates / values come from the
    compiled params (the `test_documents_unit.py` philosophy). `executed`
    records statement kinds IN ORDER so the snapshot-before-overwrite ordering
    can be pinned and the 404 path proven snapshot-free. `revisions` captures
    every sermon_doc_revisions INSERT so the snapshot's prior content + JWT
    user_id can be asserted. The clock advances on every write so the
    overwritten `updated_at` moves.
    """

    def __init__(self, docs: dict[uuid.UUID, _StoredDoc] | None = None) -> None:
        self.docs: dict[uuid.UUID, _StoredDoc] = docs or {}
        self.revisions: list[_Revision] = []
        self.executed: list[str] = []
        self.commits = 0
        self._clock = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)

    def _now(self) -> datetime:
        self._clock += timedelta(seconds=1)
        return self._clock

    async def commit(self) -> None:
        self.commits += 1

    async def execute(self, stmt: Any) -> _FakeResult:
        compiled = stmt.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        params = compiled.params

        if sql.startswith("INSERT INTO sermon_doc_revisions"):
            self.executed.append("insert_revision")
            self.revisions.append(_Revision(params))
            return _FakeResult([uuid.uuid4()])

        if sql.startswith("UPDATE documents SET"):
            self.executed.append("update")
            doc = self.docs.get(params["document_id_1"])
            if doc is None or doc.user_id != params["user_id_1"] or doc.deleted_at is not None:
                return _FakeResult([])
            if "content" in params:
                doc.content = params["content"]
            if "content_text" in params:
                doc.content_text = params["content_text"]
            doc.updated_at = self._now()
            return _FakeResult([tuple(getattr(doc, col) for col in _UPDATE_TUPLE_COLS)])

        if "FROM documents" in sql:
            # The export/import gates always carry a document_id predicate
            # (the active-owned gate from _require_owned_document).
            self.executed.append("gate")
            doc = self.docs.get(params["document_id_1"])
            if doc is None or doc.user_id != params["user_id_1"]:
                return _FakeResult([])
            if "deleted_at IS NULL" in sql and doc.deleted_at is not None:
                return _FakeResult([])
            return _FakeResult([doc])

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


def _seed_doc(user_id: uuid.UUID, *, deleted: bool = False) -> tuple[uuid.UUID, _StoredDoc]:
    document_id = uuid.uuid4()
    now = datetime(2026, 6, 14, 8, 0, 0, tzinfo=UTC)
    doc = _StoredDoc(
        document_id=document_id,
        user_id=user_id,
        title="My Sermon: Grace!",
        content=PRIOR_DOC,
        content_text=PRIOR_TEXT,
        schema_version=SCHEMA_VERSION,
        created_at=now,
        updated_at=now,
        deleted_at=now if deleted else None,
    )
    return document_id, doc


# Minimal valid OOXML .docx bytes are awkward to hand-craft; the import edge
# tests that DON'T need a real pandoc round-trip stub the byte-sniff seam
# instead (see `_stub_sniff_ok`). A real .docx is only needed for the live
# end-to-end test.
def _stub_sniff_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the libmagic docx sniff pass for arbitrary bytes (mock the seam)."""

    class _FakeMagic:
        @staticmethod
        def from_buffer(_buf: bytes, *, mime: bool) -> str:  # noqa: ARG004
            return _DOCX_MIME

    monkeypatch.setattr(documents_module, "magic", _FakeMagic)


def _stub_convert_from_docx(monkeypatch: pytest.MonkeyPatch, *, returns: dict[str, Any]) -> None:
    def _convert(_b: bytes) -> dict[str, Any]:
        return returns

    monkeypatch.setattr(documents_module, "convert_from_docx", _convert)


def _never_convert_to_docx(_content: dict[str, Any]) -> bytes:
    """A convert_to_docx replacement that fails the test if it is ever called.

    Wired on the 404 / cross-tenant / soft-deleted export paths to PROVE the
    ownership gate runs BEFORE any conversion (no work, no oracle, past a 404).
    """
    msg = "convert_to_docx must not run past the gate"
    raise AssertionError(msg)


def _never_convert_from_docx(_b: bytes) -> dict[str, Any]:
    """A convert_from_docx replacement that fails the test if ever called.

    Wired on the 415 / 413 / 404 / cross-tenant import paths to PROVE the edge
    checks + ownership gate run BEFORE any conversion or snapshot.
    """
    msg = "convert_from_docx must not run past the edge checks / gate"
    raise AssertionError(msg)


# === _export_filename (pure helper) ==========================================


def test_export_filename_sanitizes_user_title() -> None:
    # Path / header-injection chars collapse to `_`; leading/trailing `_` are
    # stripped; the suffix is always .docx. No raw quote/CR/LF/`/` survives
    # into the Content-Disposition header.
    assert _export_filename("My Sermon: Grace!") == "My_Sermon__Grace.docx"
    assert _export_filename("../../etc/passwd") == ".._.._etc_passwd.docx"
    fname = _export_filename('a"b\r\nContent-Type: x')
    assert fname == "a_b__Content-Type__x.docx"
    assert '"' not in fname
    assert "\r" not in fname and "\n" not in fname
    assert "/" not in fname


def test_export_filename_empty_falls_back_to_sermon() -> None:
    assert _export_filename("") == "sermon.docx"
    assert _export_filename("!!!") == "sermon.docx"
    assert _export_filename("   ") == "sermon.docx"


# === _revision_insert_stmt (tenant seam, compile-pinned) =====================


def test_revision_insert_stmt_carries_jwt_user_and_prior_content() -> None:
    document_id = uuid.uuid4()
    user_id = uuid.uuid4()
    stmt = _revision_insert_stmt(
        document_id=document_id,
        user_id=user_id,
        content=PRIOR_DOC,
        content_text=PRIOR_TEXT,
        schema_version=SCHEMA_VERSION,
    )
    compiled = stmt.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    params = compiled.params
    assert sql.startswith("INSERT INTO sermon_doc_revisions")
    # The tenant column is the JWT-derived user_id — never a body/path value.
    assert params["user_id"] == user_id
    assert params["document_id"] == document_id
    # The snapshot holds the PRIOR content + projection + the import source.
    assert params["content"] == PRIOR_DOC
    assert params["content_text"] == PRIOR_TEXT
    assert params["source"] == "import"


# === export ==================================================================


def test_export_streams_docx_for_owned_document(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
) -> None:
    document_id, doc = _seed_doc(fake_user.user_id)
    _wire_session(monkeypatch, _FakeSession({document_id: doc}))
    captured: dict[str, Any] = {}

    def _fake_to_docx(content: dict[str, Any]) -> bytes:
        captured["content"] = content
        return b"PK\x03\x04 fake docx bytes"

    monkeypatch.setattr(documents_module, "convert_to_docx", _fake_to_docx)

    resp = client.get(f"/documents/{document_id}/export.docx")
    assert resp.status_code == 200
    assert resp.content == b"PK\x03\x04 fake docx bytes"
    assert resp.headers["content-type"] == _DOCX_MIME
    # Sanitized filename derived from the (user-controlled) title.
    assert resp.headers["content-disposition"] == 'attachment; filename="My_Sermon__Grace.docx"'
    # Export converts the canonical stored content, untouched.
    assert captured["content"] == PRIOR_DOC


@pytest.mark.parametrize(
    "bad_id",
    ["not-a-uuid", str(uuid.uuid4())],  # non-UUID garbage, nonexistent UUID
)
def test_export_404_no_oracle_and_never_converts(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    bad_id: str,
) -> None:
    # Empty store: nonexistent + non-UUID are byte-identical 404s and
    # convert_to_docx is NEVER reached.
    session = _FakeSession({})
    _wire_session(monkeypatch, session)
    monkeypatch.setattr(documents_module, "convert_to_docx", _never_convert_to_docx)

    resp = client.get(f"/documents/{bad_id}/export.docx")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Document not found."}


def test_export_cross_tenant_is_404(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A doc owned by SOMEONE ELSE: user B's export is a 404 identical to a
    # nonexistent id — no existence oracle.
    other_user = uuid.uuid4()
    document_id, doc = _seed_doc(other_user)
    _wire_session(monkeypatch, _FakeSession({document_id: doc}))
    monkeypatch.setattr(documents_module, "convert_to_docx", _never_convert_to_docx)
    resp = client.get(f"/documents/{document_id}/export.docx")
    assert resp.status_code == 404


def test_export_soft_deleted_is_404(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
) -> None:
    document_id, doc = _seed_doc(fake_user.user_id, deleted=True)
    _wire_session(monkeypatch, _FakeSession({document_id: doc}))
    monkeypatch.setattr(documents_module, "convert_to_docx", _never_convert_to_docx)
    resp = client.get(f"/documents/{document_id}/export.docx")
    assert resp.status_code == 404


def test_export_conversion_error_is_502_no_oracle(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
) -> None:
    document_id, doc = _seed_doc(fake_user.user_id)
    _wire_session(monkeypatch, _FakeSession({document_id: doc}))

    def _raise(_c: dict[str, Any]) -> bytes:
        # A long, leaky message — the route must NOT surface it.
        msg = "pandoc html->docx failed: <secret stack trace>"
        raise documents_module.ConversionError(msg)

    monkeypatch.setattr(documents_module, "convert_to_docx", _raise)
    resp = client.get(f"/documents/{document_id}/export.docx")
    assert resp.status_code == 502
    # Fixed detail — never the raw ConversionError message (no stack oracle).
    assert resp.json() == {"detail": "Document export failed."}


# === import — happy path (snapshot FIRST) ====================================


def test_import_snapshots_prior_content_before_overwrite(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
) -> None:
    document_id, doc = _seed_doc(fake_user.user_id)
    session = _FakeSession({document_id: doc})
    _wire_session(monkeypatch, session)
    _stub_sniff_ok(monkeypatch)
    _stub_convert_from_docx(monkeypatch, returns=IMPORTED_DOC)

    resp = client.post(
        f"/documents/{document_id}/import",
        files={"file": ("sermon.docx", b"any bytes (sniff stubbed)", _DOCX_MIME)},
    )
    assert resp.status_code == 200

    # --- snapshot-FIRST: exactly one revision, holding the PRIOR content ---
    assert len(session.revisions) == 1
    rev = session.revisions[0]
    assert rev.content == PRIOR_DOC
    assert rev.content_text == PRIOR_TEXT
    assert rev.schema_version == SCHEMA_VERSION
    assert rev.source == "import"
    # The snapshot's user_id is the JWT user (denormalized tenant gate),
    # never a body/path value, and matches the doc owner.
    assert rev.user_id == fake_user.user_id
    assert rev.document_id == document_id

    # --- ordering: gate, THEN insert_revision, THEN update (snapshot predates
    #     the destructive overwrite) ---
    assert session.executed == ["gate", "insert_revision", "update"]
    # The insert is committed once, atomically with the update.
    assert session.commits == 1

    # --- the response reflects the NEW content + RE-DERIVED content_text ---
    body = resp.json()
    assert body["content"] == IMPORTED_DOC
    assert body["content_text"] == IMPORTED_TEXT == derive_content_text(IMPORTED_DOC)
    # updated_at moved past the prior value (overwrite bumped it).
    assert body["updated_at"] > doc.created_at.isoformat()


def test_import_rederives_content_text_not_trusting_conversion(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
) -> None:
    # Even if the conversion output were somehow paired with a bogus
    # content_text, the route re-derives it from the content node tree — the
    # client/conversion never supplies the projection.
    document_id, doc = _seed_doc(fake_user.user_id)
    session = _FakeSession({document_id: doc})
    _wire_session(monkeypatch, session)
    _stub_sniff_ok(monkeypatch)
    imported = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "real body"}]},
        ],
    }
    _stub_convert_from_docx(monkeypatch, returns=imported)

    resp = client.post(
        f"/documents/{document_id}/import",
        files={"file": ("x.docx", b"bytes", _DOCX_MIME)},
    )
    assert resp.status_code == 200
    assert resp.json()["content_text"] == "real body"
    # The stored doc's content_text is the server projection.
    assert session.docs[document_id].content_text == "real body"


# === import — edge defenses (415 / 413) ======================================


def test_import_non_docx_is_415_before_gate_and_snapshot(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
) -> None:
    document_id, doc = _seed_doc(fake_user.user_id)
    session = _FakeSession({document_id: doc})
    _wire_session(monkeypatch, session)

    # Real libmagic sniff over plain-text bytes → not a docx → 415. No stub.
    monkeypatch.setattr(documents_module, "convert_from_docx", _never_convert_from_docx)
    resp = client.post(
        f"/documents/{document_id}/import",
        files={"file": ("evil.docx", b"this is plainly not a docx zip", _DOCX_MIME)},
    )
    assert resp.status_code == 415
    # Nothing past the edge ran: no gate, no snapshot, no update, no commit.
    assert session.executed == []
    assert session.revisions == []
    assert session.commits == 0


def test_import_oversize_is_413_before_gate_and_snapshot(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
) -> None:
    document_id, doc = _seed_doc(fake_user.user_id)
    session = _FakeSession({document_id: doc})
    _wire_session(monkeypatch, session)
    # Stub the sniff so we'd pass it IF the size check didn't fire first —
    # proving the 413 is the body-size gate, not a sniff failure.
    _stub_sniff_ok(monkeypatch)
    monkeypatch.setattr(documents_module, "convert_from_docx", _never_convert_from_docx)
    oversize = b"x" * (MAX_CONTENT_BYTES + 1)
    resp = client.post(
        f"/documents/{document_id}/import",
        files={"file": ("big.docx", oversize, _DOCX_MIME)},
    )
    assert resp.status_code == 413
    assert session.executed == []
    assert session.revisions == []
    assert session.commits == 0


def test_import_converted_json_over_cap_is_413_after_snapshot_attempt(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
) -> None:
    # The UPLOAD is within cap, but the CONVERTED JSON exceeds it — the
    # re-cap (step 5) fires AFTER the gate but BEFORE the snapshot/update, so
    # nothing is written.
    document_id, doc = _seed_doc(fake_user.user_id)
    session = _FakeSession({document_id: doc})
    _wire_session(monkeypatch, session)
    _stub_sniff_ok(monkeypatch)
    huge_text = "x" * (MAX_CONTENT_BYTES + 100)
    huge = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": huge_text}]}],
    }
    _stub_convert_from_docx(monkeypatch, returns=huge)
    resp = client.post(
        f"/documents/{document_id}/import",
        files={"file": ("x.docx", b"small upload", _DOCX_MIME)},
    )
    assert resp.status_code == 413
    # The gate ran; the snapshot/update did NOT.
    assert session.executed == ["gate"]
    assert session.revisions == []
    assert session.commits == 0


# === import — 404 matrix =====================================================


@pytest.mark.parametrize("bad_id", ["not-a-uuid", str(uuid.uuid4())])
def test_import_404_no_oracle_no_snapshot(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    bad_id: str,
) -> None:
    session = _FakeSession({})
    _wire_session(monkeypatch, session)
    _stub_sniff_ok(monkeypatch)
    monkeypatch.setattr(documents_module, "convert_from_docx", _never_convert_from_docx)
    resp = client.post(
        f"/documents/{bad_id}/import",
        files={"file": ("x.docx", b"bytes", _DOCX_MIME)},
    )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Document not found."}
    assert session.revisions == []
    assert session.commits == 0


def test_import_cross_tenant_is_404_no_snapshot(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_user = uuid.uuid4()
    document_id, doc = _seed_doc(other_user)
    session = _FakeSession({document_id: doc})
    _wire_session(monkeypatch, session)
    _stub_sniff_ok(monkeypatch)
    monkeypatch.setattr(documents_module, "convert_from_docx", _never_convert_from_docx)
    resp = client.post(
        f"/documents/{document_id}/import",
        files={"file": ("x.docx", b"bytes", _DOCX_MIME)},
    )
    assert resp.status_code == 404
    # The prior (other tenant's) doc is untouched; no snapshot minted.
    assert session.revisions == []
    assert session.docs[document_id].content == PRIOR_DOC


# === import — convert failure (502) ==========================================


def test_import_conversion_error_is_502_no_snapshot(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
) -> None:
    document_id, doc = _seed_doc(fake_user.user_id)
    session = _FakeSession({document_id: doc})
    _wire_session(monkeypatch, session)
    _stub_sniff_ok(monkeypatch)

    def _raise(_b: bytes) -> dict[str, Any]:
        # A long, leaky message — the route must NOT surface it.
        msg = "pandoc docx->html failed: <secret path>"
        raise documents_module.ConversionError(msg)

    monkeypatch.setattr(documents_module, "convert_from_docx", _raise)
    resp = client.post(
        f"/documents/{document_id}/import",
        files={"file": ("x.docx", b"bytes", _DOCX_MIME)},
    )
    assert resp.status_code == 502
    assert resp.json() == {"detail": "Document import failed."}
    # The gate ran; conversion failed BEFORE the snapshot — nothing written,
    # nothing committed, the prior doc intact.
    assert session.executed == ["gate"]
    assert session.revisions == []
    assert session.commits == 0
    assert session.docs[document_id].content == PRIOR_DOC


# === import — multipart contract =============================================


def test_import_missing_file_part_is_422(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
) -> None:
    document_id, doc = _seed_doc(fake_user.user_id)
    _wire_session(monkeypatch, _FakeSession({document_id: doc}))
    # No `file` part — FastAPI's File(...) requirement is a 422; the import
    # route takes no JSON body to smuggle user_id/document_id through.
    resp = client.post(f"/documents/{document_id}/import")
    assert resp.status_code == 422


# === import — /tmp staging is ALWAYS cleaned =================================


def _staging_dir() -> Path:
    return main_module.settings.upload_dir


def _count_import_subdirs(before: set[Path]) -> set[Path]:
    staging = _staging_dir()
    if not staging.exists():
        return set()
    return {p for p in staging.iterdir() if p.is_dir()} - before


def test_import_tmp_staging_cleaned_on_success(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
) -> None:
    document_id, doc = _seed_doc(fake_user.user_id)
    _wire_session(monkeypatch, _FakeSession({document_id: doc}))
    _stub_sniff_ok(monkeypatch)
    _stub_convert_from_docx(monkeypatch, returns=IMPORTED_DOC)

    staging = _staging_dir()
    staging.mkdir(parents=True, exist_ok=True)
    before = {p for p in staging.iterdir() if p.is_dir()}
    resp = client.post(
        f"/documents/{document_id}/import",
        files={"file": ("x.docx", b"bytes", _DOCX_MIME)},
    )
    assert resp.status_code == 200
    # No new per-import subdir survived — the finally removed staged file +
    # subdir.
    assert _count_import_subdirs(before) == set()


def test_import_tmp_staging_cleaned_on_convert_failure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
) -> None:
    document_id, doc = _seed_doc(fake_user.user_id)
    _wire_session(monkeypatch, _FakeSession({document_id: doc}))
    _stub_sniff_ok(monkeypatch)

    def _raise(_b: bytes) -> dict[str, Any]:
        raise documents_module.ConversionError("boom")

    monkeypatch.setattr(documents_module, "convert_from_docx", _raise)

    staging = _staging_dir()
    staging.mkdir(parents=True, exist_ok=True)
    before = {p for p in staging.iterdir() if p.is_dir()}
    resp = client.post(
        f"/documents/{document_id}/import",
        files={"file": ("x.docx", b"bytes", _DOCX_MIME)},
    )
    assert resp.status_code == 502
    # Even on the failure path the finally cleaned the staged subdir.
    assert _count_import_subdirs(before) == set()


# === live end-to-end round trip (skip-guarded) ===============================


def _convert_stack_available() -> bool:
    try:
        import pypandoc
    except ImportError:
        return False
    try:
        pypandoc.get_pandoc_version()
    except OSError:
        return False
    return (
        shutil.which("node") is not None
        and _NODE_CLI.exists()
        and _NODE_MODULES.is_dir()
        and _REFERENCE_DOCX.exists()
    )


_LIVE_SKIP_REASON = (
    "docx round-trip needs pandoc + Node 22 + worker/convert_node/node_modules + reference.docx"
)


@pytest.mark.skipif(not _convert_stack_available(), reason=_LIVE_SKIP_REASON)
def test_export_then_import_real_convert_roundtrip(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
) -> None:
    """A REAL pandoc+Node round trip through the API: export → import.

    No convert stub. The export route turns the stored content into actual
    .docx bytes; those bytes are POSTed straight back to the import route
    (real libmagic sniff, real pandoc, real Node leg). The import overwrites
    the doc and snapshots the prior content; we assert the recovered body
    survives and the snapshot held the original.
    """
    document_id, doc = _seed_doc(fake_user.user_id)
    session = _FakeSession({document_id: doc})
    _wire_session(monkeypatch, session)

    # 1. Export — real convert_to_docx (no stub).
    export_resp = client.get(f"/documents/{document_id}/export.docx")
    assert export_resp.status_code == 200
    assert export_resp.headers["content-type"] == _DOCX_MIME
    docx_bytes = export_resp.content
    assert docx_bytes[:2] == b"PK"  # a real OOXML zip container

    # The export GET ran its own ownership gate through this shared session;
    # reset the recorder so the import's statement ORDER is asserted clean.
    session.executed.clear()

    # 2. Import those exact bytes back — real sniff + pandoc + Node.
    import_resp = client.post(
        f"/documents/{document_id}/import",
        files={"file": ("roundtrip.docx", docx_bytes, _DOCX_MIME)},
    )
    assert import_resp.status_code == 200
    body = import_resp.json()
    # The heading + body text survive the round trip (the convert engine's
    # phase gate proves structure/citation fidelity; here we assert the API
    # surfaced a non-empty re-derived projection carrying the original text).
    assert "Grace" in body["content_text"]
    assert "By grace alone" in body["content_text"]

    # 3. Snapshot-FIRST held the ORIGINAL content, scoped to the JWT user.
    assert len(session.revisions) == 1
    rev = session.revisions[0]
    assert rev.content == PRIOR_DOC
    assert rev.user_id == fake_user.user_id
    assert session.executed == ["gate", "insert_revision", "update"]
