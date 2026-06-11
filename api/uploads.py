"""Upload + task-status routes.

``POST /upload`` accepts a multipart EPUB/PDF, content-sniffs it, streams
it to disk under ``SERMON_API_UPLOAD_DIR``, records an ``upload_tasks``
ownership row, and enqueues a Celery ingest task with the JWT-derived
``user_id``. ``GET /tasks/{task_id}`` returns the Celery
``AsyncResult.status`` (and the task result payload when ready) — for the
caller's OWN tasks only.

## Security choices

- **JWT-only ``user_id``.** Per repo-root ``CLAUDE.md``, the worker
  task's ``user_id`` argument is always ``current_user.user_id`` (the
  JWT ``sub``) — never a request body or query param.
- **Content sniff at the edge (Phase 20).** The first bytes of the body
  are libmagic-sniffed BEFORE anything touches disk; non-EPUB/PDF
  content is a 415. This replaces the Phase 10 "no format trust at the
  API" posture, whose rationale — "refusing here would just push
  attackers to a slightly different content-type header" — argued
  against trusting the *client-supplied header*. The sniff inspects the
  *bytes*, the same evidence the worker sees, so there is no header to
  vary: a script renamed ``.epub`` dies here instead of being staged to
  disk and burning a queue slot on a guaranteed-failure task. The
  worker's ``extractors.detect()`` still re-sniffs the staged file and
  stays authoritative (defense in depth; the API sniff sees only the
  head of the stream). ``_ALLOWED_UPLOAD_MIMES`` mirrors
  ``worker/extractors/extract.py:_MIME_TO_FORMAT`` — mirrored, NOT
  imported (api/ must not import ``worker.extractors``); if one side
  changes, change the other in the same PR (the
  ``storage.sanitize_filename`` precedent, ``worker/AGENTS.md``).
- **Streamed write with a size cap.** Multipart bodies stream to a
  ``SpooledTemporaryFile`` by default; we chunk-copy that into the
  upload path while counting bytes and abort at
  ``settings.upload_max_bytes`` so a malicious client can't fill the
  disk. The partial file is removed on abort.
- **Per-upload directory.** Each upload gets a UUID-named subdir of
  ``settings.upload_dir``; the user's original filename lands inside
  that dir, sanitized of path components and control characters. Two
  uploads with the same filename never collide; nothing the user types
  can escape ``settings.upload_dir``.
- **Task ownership, not task-id capability (Phase 20).** Each upload
  commits an ``upload_tasks(task_id, user_id, …)`` row and ``GET
  /tasks/{task_id}`` resolves the row scoped to the JWT user: non-owned
  and nonexistent ids are the SAME 404 (no existence oracle — the
  api/AGENTS.md cross-tenant-404 rule). The Celery backend is consulted
  only after ownership passes, which also stops Celery's
  PENDING-for-unknown-ids behavior from leaking probe feedback. Ordering
  is deliberate: the row is committed BEFORE ``send_task`` — a crash
  between the two leaves an owned row whose task never runs (shows
  PENDING; user retries), whereas send-then-commit could run a task its
  owner can never see and whose idempotency claim row is missing. The
  same row carries the worker's in-flight ``book_id`` claim that closes
  the Phase 9 orphan-vector window (``worker/ingest.py`` "Task-id
  claim").
"""

# Celery 5 ships without `py.typed`; mirror worker/celery_app.py's relaxation.
# python-magic is a thin untyped ctypes wrapper around libmagic (same
# relaxation as worker/extractors/extract.py).
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Annotated, Any

import magic
from db import UploadTask
from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import Select, select

from auth import CurrentUserDep, SessionDep
from settings import settings
from tasks_client import enqueue_ingest, task_status

router = APIRouter(tags=["uploads"])

_CHUNK_BYTES = 1 << 20  # 1 MiB
# Anything outside [A-Za-z0-9._-] becomes `_`; the worker's libmagic sniff
# doesn't trust the extension either way, but a clean basename is friendlier
# in logs.
_FILENAME_SANITIZE = re.compile(r"[^A-Za-z0-9._-]")
# Mirror of worker/extractors/extract.py:_MIME_TO_FORMAT — mirrored, not
# imported (see the module docstring). Change both sides in the same PR.
_ALLOWED_UPLOAD_MIMES = frozenset({"application/epub+zip", "application/pdf"})
# libmagic needs only the head of the stream for container/signature
# detection (EPUB's `mimetype` entry sits in the first ~100 bytes; PDF's
# `%PDF-` at offset 0). 8 KiB is comfortably past every signature we accept.
_SNIFF_BYTES = 8192


class UploadResponse(BaseModel):
    task_id: str
    upload_id: uuid.UUID
    filename: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    result: dict[str, Any] | None = None


def _sanitize_filename(raw: str | None) -> str:
    """Strip path components and unsafe characters; fall back to ``upload.bin``.

    Multipart filenames are client-supplied and untrusted — a value like
    ``../../etc/passwd`` would otherwise drop the file outside
    ``settings.upload_dir``. Backslashes are normalized to forward slashes
    first so Windows-style paths get the same treatment (``Path(...).name``
    on Linux treats backslash as a regular character); the regex then
    collapses anything that isn't a sane character class.
    """
    if not raw:
        return "upload.bin"
    base = Path(raw.replace("\\", "/")).name.lstrip(".")
    cleaned = _FILENAME_SANITIZE.sub("_", base)
    return cleaned or "upload.bin"


def _read_sniffed_head(src: UploadFile) -> bytes:
    """Read the first chunk of *src* and 415 unless it sniffs as EPUB/PDF.

    Runs BEFORE any disk write — a rejected upload never touches
    ``settings.upload_dir`` (no subdir, no staged bytes). The sniff is
    over content bytes, never the client's Content-Type header; see the
    module docstring for why this supersedes the Phase 10 posture. The
    returned head is handed to ``_stream_to_disk`` so the body is read
    exactly once.
    """
    head = src.file.read(_CHUNK_BYTES)
    mime = magic.from_buffer(head[:_SNIFF_BYTES], mime=True)
    if mime not in _ALLOWED_UPLOAD_MIMES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported upload content (sniffed {mime!r}); expected EPUB or PDF.",
        )
    return head


def _stream_to_disk(src: UploadFile, dest: Path, *, head: bytes, max_bytes: int) -> int:
    """Copy *head* + the rest of *src* to *dest*; abort + delete past *max_bytes*.

    Returns the number of bytes written on success. Raises
    ``HTTPException(413)`` and removes the partial file on overflow.
    Underlying ``UploadFile.file`` is a ``SpooledTemporaryFile`` so the
    read side is already capped to memory by Starlette's default.
    *head* is the already-read sniff chunk (``_read_sniffed_head``); it
    counts against the cap like every other chunk.
    """
    written = 0
    with dest.open("wb") as out:
        chunk = head
        while chunk:
            written += len(chunk)
            if written > max_bytes:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Upload exceeds {max_bytes} bytes.",
                )
            out.write(chunk)
            chunk = src.file.read(_CHUNK_BYTES)
    return written


def _ownership_stmt(task_id: uuid.UUID, user_id: uuid.UUID) -> Select[tuple[uuid.UUID]]:
    """Build the tenant-scoped task-ownership lookup.

    Factored out so the WHERE clause can be pinned in a unit test without
    a live database (the ``library._library_stmt`` pattern). BOTH
    predicates are load-bearing: drop ``user_id`` and any authenticated
    user can poll any task; drop ``task_id`` and the lookup is
    meaningless. ``user_id`` is ALWAYS the JWT-derived value (CLAUDE.md
    tenant invariant).
    """
    return select(UploadTask.task_id).where(
        UploadTask.task_id == task_id,
        UploadTask.user_id == user_id,
    )


@router.post("/upload", response_model=UploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload(
    current_user: CurrentUserDep,
    session: SessionDep,
    file: Annotated[UploadFile, File(...)],
) -> UploadResponse:
    """Sniff, stage, record ownership, enqueue ingest. Returns the task_id."""
    # 1. Content sniff before anything touches disk (415 on non-EPUB/PDF).
    head = _read_sniffed_head(file)

    # 2. Stage to the per-upload subdir.
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4()
    upload_subdir = settings.upload_dir / str(upload_id)
    upload_subdir.mkdir(parents=True, exist_ok=False)
    filename = _sanitize_filename(file.filename)
    dest = upload_subdir / filename
    _stream_to_disk(file, dest, head=head, max_bytes=settings.upload_max_bytes)

    # 3. Commit the ownership/idempotency row, THEN enqueue — deliberate
    #    ordering, see the module docstring ("Task ownership"). The api
    #    mints the task UUID so the row can exist before Celery does.
    task_uuid = uuid.uuid4()
    session.add(UploadTask(task_id=task_uuid, user_id=current_user.user_id, filename=filename))
    await session.commit()
    task_id = enqueue_ingest(
        path=str(dest),
        user_id=str(current_user.user_id),
        task_id=str(task_uuid),
    )
    return UploadResponse(task_id=task_id, upload_id=upload_id, filename=filename)


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(
    task_id: str,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> TaskStatusResponse:
    """Return the Celery task status for the caller's OWN task.

    Non-owned and nonexistent task ids are the same 404 — returning
    anything different for "exists under another tenant" would be an
    existence oracle (api/AGENTS.md "Common 401/403 mistakes"). The
    ownership row gates the Celery lookup: the backend reports PENDING
    for ids it has never seen, so consulting it first would turn this
    route into a universal 200 prober.
    """
    not_found = HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Task not found.",
    )
    try:
        task_uuid = uuid.UUID(task_id)
    except ValueError as exc:
        # Not a UUID → cannot be an upload_tasks PK → same 404 shape.
        raise not_found from exc
    owned = await session.execute(_ownership_stmt(task_uuid, current_user.user_id))
    if owned.scalar_one_or_none() is None:
        raise not_found

    async_result = task_status(task_id)
    state = async_result.status
    payload: dict[str, Any] | None = None
    if state == "SUCCESS":
        raw = async_result.result
        # The worker task returns a JSON-friendly dict (book_id str,
        # was_duplicate bool, rows_inserted int) — see worker/tasks/ingest.py.
        # Non-dict values (None on PENDING, exceptions on FAILURE) are
        # surfaced as-is via the state field; payload stays None.
        if isinstance(raw, dict):
            payload = raw
    return TaskStatusResponse(task_id=task_id, status=state, result=payload)
