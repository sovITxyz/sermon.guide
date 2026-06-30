"""Unit tests for the external-editor link surface (Phase 45 — B4).

Pure-unit, no live infra, no live Google calls. Three layers:

- **statement builders**: every ``_xxx_stmt`` carries its ``user_id`` predicate
  (the ``test_documents_unit.py`` / ``test_integrations_unit.py`` compile-pin) —
  the mechanical tenant audit. The linked-row stmt also pins ``state='linked'``.
- **drive_client**: the access-token provider reuses a still-valid cached token
  (no httpx call), REFRESHES + re-encrypts + persists on expiry, and maps
  Google ``invalid_grant`` to ``DriveAuthError`` (the re-connect signal). httpx
  is fully stubbed.
- **routes** (``main.app`` via ``TestClient``, auth + session overridden, the
  ``test_documents_unit.py`` pattern, ``drive_client`` + ``convert`` stubbed):
  link stores a ``state='linked'`` row and a second link is a 409; status's
  ``remote_changed`` is the version cursor compare; pull inserts the
  ``source='pull'`` snapshot BEFORE the content overwrite (ordering asserted)
  and RE-DERIVES ``content_text``; a cross-tenant / nonexistent / non-UUID
  ``document_id`` is a byte-identical 404; unlink offers both modes and a
  smuggled field is a 422 (extra="forbid").
"""

# Tests exercise module-internals and pass duck-typed fakes on purpose.
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportArgumentType=false, reportUnknownVariableType=false, reportUnknownLambdaType=false, reportUnusedFunction=false, reportUnknownMemberType=false

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

import auth
import crypto_vault
import drive_client
import editor_links
import main as main_module
from drive_client import DriveAuthError, get_access_token
from editor_links import (
    _connection_stmt,
    _link_insert_stmt,
    _linked_row_stmt,
    _set_state_stmt,
    _set_version_stmt,
)
from settings import DEV_JWT_SECRET

_DUMMY_KEY = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"


# --- statement builders (the tenant compile-pin) -----------------------------


def test_connection_stmt_filters_by_user_id_and_provider() -> None:
    uid = uuid.uuid4()
    compiled = _connection_stmt(uid, "google").compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "oauth_connections.user_id =" in sql
    assert "oauth_connections.provider =" in sql
    assert uid in compiled.params.values()


def test_linked_row_stmt_filters_by_user_id_document_and_state() -> None:
    uid = uuid.uuid4()
    did = uuid.uuid4()
    compiled = _linked_row_stmt(did, uid).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    # The denormalized tenant gate AND the document scope AND the live-state gate.
    assert "editor_links.user_id =" in sql
    assert "editor_links.document_id =" in sql
    assert "editor_links.state =" in sql
    assert uid in compiled.params.values()
    assert did in compiled.params.values()
    assert "linked" in compiled.params.values()


def test_link_insert_stmt_carries_user_id_and_linked_state() -> None:
    uid = uuid.uuid4()
    did = uuid.uuid4()
    compiled = _link_insert_stmt(
        document_id=did,
        user_id=uid,
        provider="google",
        provider_file_id="file-123",
        web_url="https://docs.example/d/file-123",
        last_remote_version="7",
    ).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "INSERT INTO editor_links" in sql
    assert uid in compiled.params.values()
    assert "linked" in compiled.params.values()


def test_set_version_stmt_filters_by_user_id() -> None:
    uid = uuid.uuid4()
    lid = uuid.uuid4()
    compiled = _set_version_stmt(link_id=lid, user_id=uid, last_remote_version="9").compile(
        dialect=postgresql.dialect(),
    )
    sql = str(compiled)
    assert "UPDATE editor_links" in sql
    assert "editor_links.user_id =" in sql
    assert uid in compiled.params.values()


def test_set_state_stmt_filters_by_user_id() -> None:
    uid = uuid.uuid4()
    lid = uuid.uuid4()
    compiled = _set_state_stmt(link_id=lid, user_id=uid, state="error").compile(
        dialect=postgresql.dialect(),
    )
    sql = str(compiled)
    assert "UPDATE editor_links" in sql
    assert "editor_links.user_id =" in sql
    assert uid in compiled.params.values()


# --- drive_client access-token provider --------------------------------------


class _FakeConn:
    def __init__(
        self,
        *,
        token_expiry: datetime | None,
        access_ct: bytes | None,
        refresh_ct: bytes,
    ) -> None:
        self.id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.provider = "google"
        self.provider_account_email = "a@b.com"
        self.access_token_ciphertext = access_ct
        self.refresh_token_ciphertext = refresh_ct
        self.token_expiry = token_expiry


class _TokenSession:
    """Captures the persist UPDATE so the refresh path can be asserted."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.commits = 0

    async def execute(self, stmt: Any) -> None:
        self.executed.append(str(stmt.compile(dialect=postgresql.dialect())))

    async def commit(self) -> None:
        self.commits += 1


class _FakeResp:
    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeAsyncClient:
    """Records the token POST and returns a scripted response."""

    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp
        self.posted = False

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    async def post(self, *_a: Any, **_k: Any) -> _FakeResp:
        self.posted = True
        return self._resp


@pytest.fixture
def _enc_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto_vault.settings, "token_enc_key", _DUMMY_KEY)


@pytest.fixture
def _google_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # drive_client.get_access_token's refresh leg calls
    # integrations._require_google_configured for the client id/secret.
    import integrations

    monkeypatch.setattr(integrations.settings, "google_client_id", "cid")
    monkeypatch.setattr(integrations.settings, "google_client_secret", "csecret")


async def test_get_access_token_reuses_valid_cached_token(_enc_key: None) -> None:
    """A still-valid cached access token is decrypted + returned — NO httpx call."""
    conn = _FakeConn(
        token_expiry=datetime.now(tz=UTC) + timedelta(hours=1),
        access_ct=crypto_vault.encrypt("cached-access-token"),
        refresh_ct=crypto_vault.encrypt("refresh"),
    )
    session = _TokenSession()
    token = await get_access_token(conn, session)  # type: ignore[arg-type]
    assert token == "cached-access-token"
    # Cache hit: nothing persisted, nothing committed.
    assert session.executed == []
    assert session.commits == 0


async def test_get_access_token_refreshes_and_persists(
    _enc_key: None,
    _google_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired access token forces a refresh; the new token is re-encrypted + saved."""
    conn = _FakeConn(
        token_expiry=datetime.now(tz=UTC) - timedelta(minutes=5),  # expired
        access_ct=crypto_vault.encrypt("old-access-token"),
        refresh_ct=crypto_vault.encrypt("the-refresh-token"),
    )
    session = _TokenSession()
    fake_client = _FakeAsyncClient(
        _FakeResp(200, {"access_token": "fresh-access-token", "expires_in": 3600}),
    )
    monkeypatch.setattr(drive_client.httpx, "AsyncClient", lambda *a, **k: fake_client)  # noqa: ARG005

    token = await get_access_token(conn, session)  # type: ignore[arg-type]
    assert token == "fresh-access-token"
    assert fake_client.posted is True
    # The refreshed access token was persisted on the SAME row (tenant-scoped).
    assert any("UPDATE oauth_connections" in sql for sql in session.executed)
    assert session.commits == 1


async def test_get_access_token_invalid_grant_raises_auth_error(
    _enc_key: None,
    _google_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revoked/expired refresh token (invalid_grant) -> DriveAuthError (re-connect)."""
    conn = _FakeConn(
        token_expiry=None,
        access_ct=None,
        refresh_ct=crypto_vault.encrypt("revoked-refresh-token"),
    )
    session = _TokenSession()
    fake_client = _FakeAsyncClient(_FakeResp(400, {"error": "invalid_grant"}))
    monkeypatch.setattr(drive_client.httpx, "AsyncClient", lambda *a, **k: fake_client)  # noqa: ARG005

    with pytest.raises(DriveAuthError):
        await get_access_token(conn, session)  # type: ignore[arg-type]
    # Nothing persisted on the failure path.
    assert session.commits == 0


# --- drive_client: upload multipart + SSRF-safe file path --------------------


def test_build_related_multipart_is_multipart_related_with_both_parts() -> None:
    """The upload body MUST be multipart/related (NOT form-data) with both parts.

    Drive's uploadType=multipart rejects multipart/form-data; the metadata part
    requests the native-doc mimeType so the docx is converted on upload.
    """
    body, content_type = drive_client._build_related_multipart("My Sermon", b"PKdocxbytes")
    assert content_type.startswith("multipart/related; boundary=")
    assert b"application/vnd.google-apps.document" in body
    assert b'"name": "My Sermon"' in body
    assert b"application/vnd.openxmlformats-officedocument.wordprocessingml.document" in body
    assert b"PKdocxbytes" in body


def test_file_path_percent_encodes_the_segment_no_url_escape() -> None:
    """A hostile file id cannot escape the fixed files/{id} path (SSRF guard)."""
    path = drive_client._file_path("../../evil?x=1")
    assert path.startswith("https://www.googleapis.com/drive/v3/files/")
    # No raw slash / query char survives into the path segment.
    assert "/files/../" not in path
    assert "?x=1" not in path
    assert "%2F" in path or "%3F" in path


# --- route layer -------------------------------------------------------------


class _FakeUser:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()


class _FakeDocument:
    def __init__(self, *, user_id: uuid.UUID) -> None:
        self.document_id = uuid.uuid4()
        self.user_id = user_id
        self.title = "On Grace"
        self.content: dict[str, object] = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Original body."}]},
            ],
        }
        self.content_text = "Original body."
        self.schema_version = 1
        # Phase 50 scope columns ride through the _update_stmt RETURNING; the
        # Google-Docs pull/unlink paths never change them.
        self.scope_book_ids: list[str] = []
        self.scope_collection_ids: list[str] = []
        self.deleted_at: datetime | None = None
        self.created_at = datetime(2026, 6, 22, 12, 0, 0, tzinfo=UTC)
        self.updated_at = self.created_at


class _FakeConnRow:
    def __init__(self, user_id: uuid.UUID) -> None:
        self.id = uuid.uuid4()
        self.user_id = user_id
        self.provider = "google"
        self.provider_account_email = "preacher@example.com"
        self.access_token_ciphertext: bytes | None = None
        self.refresh_token_ciphertext = b"ct"
        self.token_expiry: datetime | None = None


class _FakeLinkRow:
    def __init__(self, *, document_id: uuid.UUID, user_id: uuid.UUID) -> None:
        self.id = uuid.uuid4()
        self.document_id = document_id
        self.user_id = user_id
        self.provider = "google"
        self.provider_file_id = "drive-file-123"
        self.web_url = "https://docs.google.com/document/d/drive-file-123/edit"
        self.state = "linked"
        self.last_remote_version = "5"
        self.created_at = datetime(2026, 6, 22, 12, 0, 0, tzinfo=UTC)
        self.updated_at = self.created_at


class _Scalar:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def one(self) -> Any:
        return self._value


class _RouteSession:
    """A statement-routing fake session, keyed on the compiled SQL.

    Tracks an ordered ``log`` of (op) so the pull test can assert the snapshot
    INSERT precedes the documents UPDATE. ``documents`` / ``oauth_connections``
    / ``editor_links`` rows are configured by the test.
    """

    def __init__(
        self,
        *,
        document: _FakeDocument | None,
        connection: _FakeConnRow | None,
        link: _FakeLinkRow | None,
    ) -> None:
        self.document = document
        self.connection = connection
        self.link = link
        self.log: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.inserted_link = False
        self.revision_sources: list[str] = []
        self.updated_content: dict[str, object] | None = None
        self.updated_content_text: str | None = None
        self.state_set: str | None = None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def execute(self, stmt: Any) -> _Scalar:  # noqa: PLR0911
        compiled = stmt.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        params = compiled.params

        # documents owned-active lookup (the _require_owned_document gate).
        if "FROM documents" in sql and "SELECT" in sql:
            self.log.append("select_document")
            if self.document is None:
                return _Scalar(None)
            uid = params.get("user_id_1")
            return _Scalar(self.document if self.document.user_id == uid else None)

        if "FROM oauth_connections" in sql:
            self.log.append("select_connection")
            return _Scalar(self.connection)

        if "FROM editor_links" in sql and sql.strip().upper().startswith("SELECT"):
            self.log.append("select_link")
            return _Scalar(self.link)

        if "INSERT INTO editor_links" in sql:
            self.log.append("insert_link")
            self.inserted_link = True
            return _Scalar(uuid.uuid4())

        if "INSERT INTO sermon_doc_revisions" in sql:
            self.log.append("insert_revision")
            self.revision_sources.append(params.get("source"))
            return _Scalar(uuid.uuid4())

        if "UPDATE documents" in sql:
            self.log.append("update_document")
            self.updated_content = params.get("content")
            self.updated_content_text = params.get("content_text")
            assert self.document is not None
            return _Scalar(
                (
                    self.document.document_id,
                    self.document.title,
                    self.updated_content,
                    self.updated_content_text,
                    self.document.schema_version,
                    self.document.scope_book_ids,
                    self.document.scope_collection_ids,
                    self.document.created_at,
                    datetime.now(tz=UTC),
                ),
            )

        if "UPDATE editor_links" in sql:
            self.log.append("update_link")
            if "state" in params:
                self.state_set = params.get("state")
            return _Scalar(None)

        msg = f"unexpected statement: {sql}"
        raise AssertionError(msg)


@pytest.fixture
def fake_user() -> _FakeUser:
    return _FakeUser()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, fake_user: _FakeUser) -> TestClient:
    monkeypatch.setattr(main_module.settings, "env", "dev")
    monkeypatch.setattr(main_module.settings, "jwt_secret", DEV_JWT_SECRET)
    monkeypatch.setattr(crypto_vault.settings, "token_enc_key", _DUMMY_KEY)
    monkeypatch.setitem(
        main_module.app.dependency_overrides,
        auth.get_current_user,
        lambda: fake_user,
    )
    return TestClient(main_module.app)


def _wire_session(monkeypatch: pytest.MonkeyPatch, session: _RouteSession) -> None:
    async def _fake_session() -> Any:
        return session

    monkeypatch.setitem(main_module.app.dependency_overrides, auth._session, _fake_session)


def _stub_drive(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    """Stub every drive_client + convert call the routes touch (no live Google)."""

    async def _token(*_a: Any, **_k: Any) -> str:
        return "access-token"

    async def _upload(*_a: Any, **_k: Any) -> str:
        return "drive-file-123"

    async def _web(*_a: Any, **_k: Any) -> str:
        return "https://docs.google.com/document/d/drive-file-123/edit"

    async def _version(*_a: Any, **_k: Any) -> str:
        return overrides.get("version", "5")

    async def _export(*_a: Any, **_k: Any) -> str:
        return overrides.get("markdown", "# Pulled\n\nNew remote body.\n")

    async def _delete(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(editor_links.drive_client, "get_access_token", _token)
    monkeypatch.setattr(editor_links.drive_client, "upload_with_conversion", _upload)
    monkeypatch.setattr(editor_links.drive_client, "get_web_view_link", _web)
    monkeypatch.setattr(editor_links.drive_client, "get_version", _version)
    monkeypatch.setattr(editor_links.drive_client, "export_markdown", _export)
    monkeypatch.setattr(editor_links.drive_client, "delete_file", _delete)
    # convert_to_docx / convert_from_markdown — never shell out in unit tests.
    monkeypatch.setattr(editor_links, "convert_to_docx", lambda _c: b"PKfake-docx")
    monkeypatch.setattr(
        editor_links,
        "convert_from_markdown",
        lambda _m: {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "New remote body."}]},
            ],
        },
    )


def test_link_creates_linked_row(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    fake_user = main_module.app.dependency_overrides[auth.get_current_user]()
    document = _FakeDocument(user_id=fake_user.user_id)
    session = _RouteSession(
        document=document,
        connection=_FakeConnRow(fake_user.user_id),
        link=None,
    )
    _wire_session(monkeypatch, session)
    _stub_drive(monkeypatch)

    resp = client.post(f"/documents/{document.document_id}/editor-link")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "linked"
    assert body["web_url"].startswith("https://docs.google.com/")
    assert body["last_remote_version"] == "5"
    assert session.inserted_link is True


def test_second_link_while_linked_is_409(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    fake_user = main_module.app.dependency_overrides[auth.get_current_user]()
    document = _FakeDocument(user_id=fake_user.user_id)
    link = _FakeLinkRow(document_id=document.document_id, user_id=fake_user.user_id)
    session = _RouteSession(
        document=document,
        connection=_FakeConnRow(fake_user.user_id),
        link=link,  # already linked
    )
    _wire_session(monkeypatch, session)
    _stub_drive(monkeypatch)

    resp = client.post(f"/documents/{document.document_id}/editor-link")
    assert resp.status_code == 409
    # The pre-check fired — no row was inserted.
    assert session.inserted_link is False


def test_link_cross_tenant_document_is_404(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    # Document owned by ANOTHER user — the gate returns None.
    other_doc = _FakeDocument(user_id=uuid.uuid4())
    session = _RouteSession(document=other_doc, connection=None, link=None)
    _wire_session(monkeypatch, session)
    _stub_drive(monkeypatch)

    resp = client.post(f"/documents/{other_doc.document_id}/editor-link")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found."


def test_link_non_uuid_document_is_404(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _RouteSession(document=None, connection=None, link=None)
    _wire_session(monkeypatch, session)
    _stub_drive(monkeypatch)
    resp = client.post("/documents/not-a-uuid/editor-link")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found."


def test_status_remote_changed_true_when_version_differs(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    fake_user = main_module.app.dependency_overrides[auth.get_current_user]()
    document = _FakeDocument(user_id=fake_user.user_id)
    link = _FakeLinkRow(document_id=document.document_id, user_id=fake_user.user_id)
    link.last_remote_version = "5"
    session = _RouteSession(
        document=document,
        connection=_FakeConnRow(fake_user.user_id),
        link=link,
    )
    _wire_session(monkeypatch, session)
    _stub_drive(monkeypatch, version="9")  # remote advanced past the stored cursor

    resp = client.get(f"/documents/{document.document_id}/editor-link/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["remote_changed"] is True
    assert body["state"] == "linked"
    assert body["provider_account_email"] == "preacher@example.com"


def test_status_remote_changed_false_when_version_matches(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    fake_user = main_module.app.dependency_overrides[auth.get_current_user]()
    document = _FakeDocument(user_id=fake_user.user_id)
    link = _FakeLinkRow(document_id=document.document_id, user_id=fake_user.user_id)
    link.last_remote_version = "5"
    session = _RouteSession(
        document=document,
        connection=_FakeConnRow(fake_user.user_id),
        link=link,
    )
    _wire_session(monkeypatch, session)
    _stub_drive(monkeypatch, version="5")  # unchanged

    resp = client.get(f"/documents/{document.document_id}/editor-link/status")
    assert resp.status_code == 200
    assert resp.json()["remote_changed"] is False


def test_status_no_linked_row_is_404(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    fake_user = main_module.app.dependency_overrides[auth.get_current_user]()
    document = _FakeDocument(user_id=fake_user.user_id)
    session = _RouteSession(
        document=document,
        connection=_FakeConnRow(fake_user.user_id),
        link=None,  # owned doc, but no live link
    )
    _wire_session(monkeypatch, session)
    _stub_drive(monkeypatch)
    resp = client.get(f"/documents/{document.document_id}/editor-link/status")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found."


def test_pull_snapshots_before_overwrite_and_rederives_text(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    fake_user = main_module.app.dependency_overrides[auth.get_current_user]()
    document = _FakeDocument(user_id=fake_user.user_id)
    link = _FakeLinkRow(document_id=document.document_id, user_id=fake_user.user_id)
    session = _RouteSession(
        document=document,
        connection=_FakeConnRow(fake_user.user_id),
        link=link,
    )
    _wire_session(monkeypatch, session)
    _stub_drive(monkeypatch, version="11")

    resp = client.post(f"/documents/{document.document_id}/editor-link/pull")
    assert resp.status_code == 200

    # THE ordering gate: the source='pull' snapshot INSERT precedes the
    # documents content overwrite in the same transaction.
    assert "insert_revision" in session.log
    assert "update_document" in session.log
    assert session.log.index("insert_revision") < session.log.index("update_document")
    assert session.revision_sources == ["pull"]

    # content_text is SERVER-re-derived from the converted content, never
    # trusted from the conversion output.
    assert session.updated_content_text == "New remote body."
    body = resp.json()
    assert body["content_text"] == "New remote body."


def test_unlink_keep_app_leaves_content_untouched(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    fake_user = main_module.app.dependency_overrides[auth.get_current_user]()
    document = _FakeDocument(user_id=fake_user.user_id)
    link = _FakeLinkRow(document_id=document.document_id, user_id=fake_user.user_id)
    session = _RouteSession(
        document=document,
        connection=_FakeConnRow(fake_user.user_id),
        link=link,
    )
    _wire_session(monkeypatch, session)
    _stub_drive(monkeypatch)

    resp = client.post(
        f"/documents/{document.document_id}/editor-link/unlink",
        json={"mode": "keep-app"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "unlinked"
    assert session.state_set == "unlinked"
    # keep-app: NO content overwrite, NO snapshot.
    assert "update_document" not in session.log
    assert "insert_revision" not in session.log


def test_unlink_pull_final_snapshots_and_overwrites(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    fake_user = main_module.app.dependency_overrides[auth.get_current_user]()
    document = _FakeDocument(user_id=fake_user.user_id)
    link = _FakeLinkRow(document_id=document.document_id, user_id=fake_user.user_id)
    session = _RouteSession(
        document=document,
        connection=_FakeConnRow(fake_user.user_id),
        link=link,
    )
    _wire_session(monkeypatch, session)
    _stub_drive(monkeypatch, version="11")

    resp = client.post(
        f"/documents/{document.document_id}/editor-link/unlink",
        json={"mode": "pull-final"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "unlinked"
    # pull-final ran the pull pipeline: snapshot-first then overwrite, THEN detach.
    assert session.revision_sources == ["pull"]
    assert "update_document" in session.log
    assert session.log.index("insert_revision") < session.log.index("update_document")
    assert session.state_set == "unlinked"


def test_unlink_smuggled_field_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    fake_user = main_module.app.dependency_overrides[auth.get_current_user]()
    document = _FakeDocument(user_id=fake_user.user_id)
    session = _RouteSession(document=document, connection=None, link=None)
    _wire_session(monkeypatch, session)
    _stub_drive(monkeypatch)
    resp = client.post(
        f"/documents/{document.document_id}/editor-link/unlink",
        json={"mode": "keep-app", "user_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 422


def test_unlink_bad_mode_is_422(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    fake_user = main_module.app.dependency_overrides[auth.get_current_user]()
    document = _FakeDocument(user_id=fake_user.user_id)
    session = _RouteSession(document=document, connection=None, link=None)
    _wire_session(monkeypatch, session)
    _stub_drive(monkeypatch)
    resp = client.post(
        f"/documents/{document.document_id}/editor-link/unlink",
        json={"mode": "delete-everything"},
    )
    assert resp.status_code == 422
