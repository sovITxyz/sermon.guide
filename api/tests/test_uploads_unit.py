"""Unit tests for the upload + task-status routes (Phase 20 posture).

Pure-unit, no live infra. Route tests boot ``main.app`` through
``with TestClient(app):`` (lifespan runs; dev posture monkeypatched as
settings ATTRIBUTES, the suite convention) and replace the I/O seams:

- ``auth.get_current_user`` / ``auth._session`` via
  ``app.dependency_overrides`` (the ``test_ratelimit_unit.py`` pattern);
- ``uploads.enqueue_ingest`` / ``uploads.task_status`` via monkeypatch on
  the uploads module (the ``readyz._probe_*`` convention).

What this file pins:

- filename sanitization (path traversal, unsafe chars, dotfiles);
- the Phase 20 edge content sniff: real libmagic over crafted bytes —
  EPUB/PDF pass, a script renamed ``.epub`` is a 415 with NOTHING staged
  to disk, no DB row, no enqueue;
- the commit-BEFORE-send ordering on ``POST /upload`` (crash between the
  two must leave an owned row, never an unowned running task);
- the ``GET /tasks/{task_id}`` ownership matrix: owner 200; other user,
  unknown UUID, and non-UUID garbage are the SAME 404 — and the Celery
  backend is never consulted on the 404 paths (its PENDING-for-unknown-
  ids behavior must not leak through);
- the ownership statement filters by BOTH task_id and the JWT user_id
  (the ``test_library_unit.py`` compile-pin pattern).
"""

# Tests exercise module-internals and pass duck-typed fakes (e.g. _FakeUpload
# where the helper annotates UploadFile) on purpose.
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportArgumentType=false

from __future__ import annotations

import io
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

import auth
import main as main_module
import uploads
from settings import DEV_JWT_SECRET
from uploads import _ownership_stmt, _read_sniffed_head, _sanitize_filename

# --- crafted upload bytes ----------------------------------------------------

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
SCRIPT_BYTES = b"#!/bin/sh\necho pwned\n"


def _epub_bytes() -> bytes:
    """Minimal spec-shaped EPUB: `mimetype` first, STORED (libmagic's probe)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("OEBPS/content.opf", "<package/>")
    return buf.getvalue()


# --- fakes -------------------------------------------------------------------


class _FakeUser:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()


class _FakeScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeSession:
    """Duck-typed AsyncSession: records adds/commits, serves ownership rows.

    ``owned_task_ids`` is the set of task UUIDs the ownership SELECT will
    report as owned; ``calls`` records the add/commit/execute order so the
    commit-before-enqueue contract can be asserted.
    """

    def __init__(self, owned_task_ids: set[uuid.UUID] | None = None) -> None:
        self.owned_task_ids = owned_task_ids or set()
        self.added: list[Any] = []
        self.calls: list[str] = []

    def add(self, obj: Any) -> None:
        self.calls.append("add")
        self.added.append(obj)

    async def commit(self) -> None:
        self.calls.append("commit")

    async def execute(self, stmt: Any) -> _FakeScalarResult:
        self.calls.append("execute")
        # Resolve ownership the way the DB would: the compiled params carry
        # the task_id + user_id predicates the route bound.
        params = stmt.compile(dialect=postgresql.dialect()).params
        bound_uuids = {v for v in params.values() if isinstance(v, uuid.UUID)}
        return _FakeScalarResult(next(iter(bound_uuids & self.owned_task_ids), None))


class _RecordingEnqueue:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.calls: list[dict[str, str]] = []

    def __call__(self, *, path: str, user_id: str, task_id: str) -> str:
        self.session.calls.append("enqueue")
        self.calls.append({"path": path, "user_id": user_id, "task_id": task_id})
        return task_id


class _FakeAsyncResult:
    def __init__(self, status: str, result: Any = None) -> None:
        self.status = status
        self.result = result


@pytest.fixture
def fake_user() -> _FakeUser:
    return _FakeUser()


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
    tmp_path: Path,
) -> TestClient:
    """Dev-posture TestClient with auth + session overridden."""
    monkeypatch.setattr(main_module.settings, "env", "dev")
    monkeypatch.setattr(main_module.settings, "jwt_secret", DEV_JWT_SECRET)
    monkeypatch.setattr(uploads.settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setitem(
        main_module.app.dependency_overrides,
        auth.get_current_user,
        lambda: fake_user,
    )
    return TestClient(main_module.app)


def _wire_session(
    monkeypatch: pytest.MonkeyPatch,
    session: _FakeSession,
) -> None:
    async def _fake_session() -> Any:
        return session

    monkeypatch.setitem(main_module.app.dependency_overrides, auth._session, _fake_session)


# --- filename sanitization (pre-Phase 20 pins, kept) -------------------------


def test_sanitize_strips_path_traversal() -> None:
    # Multipart filenames are client-supplied; a `../../etc/passwd` value
    # without sanitization would drop the file outside settings.upload_dir.
    assert _sanitize_filename("../../etc/passwd") == "passwd"
    assert _sanitize_filename("/abs/path/book.epub") == "book.epub"
    # Backslashes are normalized to forward slashes before Path.name so
    # Windows-style paths get fully stripped, not just collapsed.
    assert _sanitize_filename("..\\..\\windows\\sys.epub") == "sys.epub"


def test_sanitize_collapses_unsafe_chars() -> None:
    assert _sanitize_filename("naïve $book; rm -rf.epub") == "na_ve__book__rm_-rf.epub"


def test_sanitize_handles_empty_and_dotfile() -> None:
    assert _sanitize_filename(None) == "upload.bin"
    assert _sanitize_filename("") == "upload.bin"
    # Leading dots are stripped so an attacker can't smuggle hidden files.
    assert _sanitize_filename(".hidden") == "hidden"
    assert _sanitize_filename("...") == "upload.bin"


# --- content sniff (Phase 20, helper level — real libmagic) ------------------


class _FakeUpload:
    def __init__(self, data: bytes) -> None:
        self.file = io.BytesIO(data)


def test_sniff_accepts_pdf_and_epub() -> None:
    assert _read_sniffed_head(_FakeUpload(PDF_BYTES)).startswith(b"%PDF")
    assert _read_sniffed_head(_FakeUpload(_epub_bytes())).startswith(b"PK")


@pytest.mark.parametrize("data", [SCRIPT_BYTES, b"", b"GIF89a not a book"])
def test_sniff_rejects_non_book_bytes(data: bytes) -> None:
    with pytest.raises(HTTPException) as excinfo:
        _read_sniffed_head(_FakeUpload(data))
    assert excinfo.value.status_code == 415


# --- POST /upload route ------------------------------------------------------


def test_upload_renamed_script_rejected_before_staging(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    """A script renamed .epub: 415, nothing staged, no row, no enqueue."""
    session = _FakeSession()
    _wire_session(monkeypatch, session)
    enqueue = _RecordingEnqueue(session)
    monkeypatch.setattr(uploads, "enqueue_ingest", enqueue)

    with client:
        response = client.post(
            "/upload",
            files={"file": ("malicious.epub", SCRIPT_BYTES, "application/epub+zip")},
        )

    assert response.status_code == 415
    # Nothing touched disk — the sniff runs before mkdir/stream.
    assert not uploads.settings.upload_dir.exists()
    assert session.added == []
    assert enqueue.calls == []


def test_upload_happy_path_commits_ownership_before_enqueue(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_user: _FakeUser,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)
    enqueue = _RecordingEnqueue(session)
    monkeypatch.setattr(uploads, "enqueue_ingest", enqueue)

    with client:
        response = client.post(
            "/upload",
            files={"file": ("book.pdf", PDF_BYTES, "application/pdf")},
        )

    assert response.status_code == 202
    body = response.json()

    # Ownership row: JWT-derived user_id, task_id matching the response.
    assert len(session.added) == 1
    row = session.added[0]
    assert row.user_id == fake_user.user_id
    assert str(row.task_id) == body["task_id"]
    assert row.filename == "book.pdf"

    # Deliberate ordering: the row COMMITS before send_task — a crash
    # between the two leaves an owned PENDING row, never an unowned task.
    assert session.calls.index("commit") < session.calls.index("enqueue")

    # The enqueue carried the api-minted task_id + JWT user_id.
    assert enqueue.calls == [
        {
            "path": str(uploads.settings.upload_dir / body["upload_id"] / "book.pdf"),
            "user_id": str(fake_user.user_id),
            "task_id": body["task_id"],
        },
    ]

    # The staged file carries the full body.
    staged = uploads.settings.upload_dir / body["upload_id"] / "book.pdf"
    assert staged.read_bytes() == PDF_BYTES


def test_upload_repost_mints_fresh_task_per_post(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    """Re-POST of identical content: two owned rows, two distinct task ids.

    Convergence of the duplicate CONTENT is the worker's job (MinHash
    dedup + the task-id claim); the api's idempotency unit is the task.
    """
    session = _FakeSession()
    _wire_session(monkeypatch, session)
    enqueue = _RecordingEnqueue(session)
    monkeypatch.setattr(uploads, "enqueue_ingest", enqueue)

    with client:
        first = client.post("/upload", files={"file": ("b.pdf", PDF_BYTES, "application/pdf")})
        second = client.post("/upload", files={"file": ("b.pdf", PDF_BYTES, "application/pdf")})

    assert first.status_code == second.status_code == 202
    assert first.json()["task_id"] != second.json()["task_id"]
    assert len(session.added) == 2
    assert len(enqueue.calls) == 2


# --- GET /tasks/{task_id} ownership matrix -----------------------------------


def test_task_status_owner_sees_status(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    task_id = uuid.uuid4()
    _wire_session(monkeypatch, _FakeSession(owned_task_ids={task_id}))
    payload = {"book_id": str(uuid.uuid4()), "was_duplicate": False, "rows_inserted": 12}
    monkeypatch.setattr(
        uploads,
        "task_status",
        lambda _tid="": _FakeAsyncResult("SUCCESS", payload),
    )

    with client:
        response = client.get(f"/tasks/{task_id}")

    assert response.status_code == 200
    assert response.json() == {
        "task_id": str(task_id),
        "status": "SUCCESS",
        "result": payload,
    }


@pytest.mark.parametrize(
    "path_task_id",
    [
        str(uuid.uuid4()),  # well-formed but not in upload_tasks (or another user's)
        "not-a-uuid",  # garbage — cannot be an upload_tasks PK
    ],
)
def test_task_status_unknown_and_garbage_ids_404_without_backend(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    path_task_id: str,
) -> None:
    """Unknown/garbage ids 404 and NEVER reach Celery.

    The result backend reports PENDING for ids it has never seen —
    consulting it before ownership would make this route a universal
    200 prober.
    """
    _wire_session(monkeypatch, _FakeSession(owned_task_ids=set()))
    backend_calls: list[str] = []

    def _spy(tid: str) -> _FakeAsyncResult:
        backend_calls.append(tid)
        return _FakeAsyncResult("PENDING")

    monkeypatch.setattr(uploads, "task_status", _spy)

    with client:
        response = client.get(f"/tasks/{path_task_id}")

    assert response.status_code == 404
    assert backend_calls == []


def test_task_status_other_users_task_is_same_404(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    """Cross-tenant poll: 404 with the SAME body as a nonexistent id."""
    # The fake session reports no ownership for the caller — exactly what
    # the real tenant-scoped SELECT returns for another user's task row.
    _wire_session(monkeypatch, _FakeSession(owned_task_ids=set()))
    monkeypatch.setattr(uploads, "task_status", lambda _tid="": _FakeAsyncResult("SUCCESS", {}))

    with client:
        cross_tenant = client.get(f"/tasks/{uuid.uuid4()}")
        nonexistent = client.get(f"/tasks/{uuid.uuid4()}")

    assert cross_tenant.status_code == nonexistent.status_code == 404
    assert cross_tenant.json() == nonexistent.json()  # no existence oracle


# --- ownership statement compile pin (tenant audit) ---------------------------


def test_ownership_stmt_filters_by_task_and_user() -> None:
    task_id, user_id = uuid.uuid4(), uuid.uuid4()
    compiled = _ownership_stmt(task_id, user_id).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    # Both predicates are load-bearing: task_id alone would let any
    # authenticated user poll any task (the pre-Phase 20 capability model).
    assert "upload_tasks.task_id =" in sql
    assert "upload_tasks.user_id =" in sql
    assert set(compiled.params.values()) == {task_id, user_id}
