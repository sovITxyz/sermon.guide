"""Upload + task-status routes.

``POST /upload`` accepts a multipart EPUB/PDF, streams it to disk under
``SERMON_API_UPLOAD_DIR``, and enqueues a Celery ingest task with the
JWT-derived ``user_id``. ``GET /tasks/{task_id}`` returns the Celery
``AsyncResult.status`` (and the task result payload when ready).

## Security choices

- **JWT-only ``user_id``.** Per repo-root ``CLAUDE.md``, the worker
  task's ``user_id`` argument is always ``current_user.user_id`` (the
  JWT ``sub``) — never a request body or query param.
- **No format trust at the API.** The worker's
  ``worker/extractors/__init__.py:detect`` sniffs MIME via libmagic;
  the API does not pre-validate. Refusing here would just push attackers
  to a slightly different content-type header without changing what the
  worker sees.
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
- **Task-id capability model.** Celery generates random UUID task IDs
  (122 bits of randomness); ``GET /tasks/{task_id}`` requires auth but
  does not check task ownership beyond that. The task_id is the
  capability. Phase 11+ should add an ``upload_tasks`` mapping when
  the user_library/search routes land; documented in ``api/AGENTS.md``.
"""

# Celery 5 ships without `py.typed`; mirror worker/celery_app.py's relaxation.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from auth import CurrentUserDep
from settings import settings
from tasks_client import enqueue_ingest, task_status

router = APIRouter(tags=["uploads"])

_CHUNK_BYTES = 1 << 20  # 1 MiB
# Anything outside [A-Za-z0-9._-] becomes `_`; the worker's libmagic sniff
# doesn't trust the extension either way, but a clean basename is friendlier
# in logs.
_FILENAME_SANITIZE = re.compile(r"[^A-Za-z0-9._-]")


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


def _stream_to_disk(src: UploadFile, dest: Path, *, max_bytes: int) -> int:
    """Copy *src* to *dest* in fixed chunks; abort + delete past *max_bytes*.

    Returns the number of bytes written on success. Raises
    ``HTTPException(413)`` and removes the partial file on overflow.
    Underlying ``UploadFile.file`` is a ``SpooledTemporaryFile`` so the
    read side is already capped to memory by Starlette's default.
    """
    written = 0
    with dest.open("wb") as out:
        while True:
            chunk = src.file.read(_CHUNK_BYTES)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Upload exceeds {max_bytes} bytes.",
                )
            out.write(chunk)
    return written


@router.post("/upload", response_model=UploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload(
    current_user: CurrentUserDep,
    file: Annotated[UploadFile, File(...)],
) -> UploadResponse:
    """Save the upload + enqueue ingest. Returns the Celery task_id."""
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4()
    upload_subdir = settings.upload_dir / str(upload_id)
    upload_subdir.mkdir(parents=True, exist_ok=False)

    filename = _sanitize_filename(file.filename)
    dest = upload_subdir / filename
    _stream_to_disk(file, dest, max_bytes=settings.upload_max_bytes)

    task_id = enqueue_ingest(path=str(dest), user_id=str(current_user.user_id))
    return UploadResponse(task_id=task_id, upload_id=upload_id, filename=filename)


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(
    task_id: str,
    current_user: CurrentUserDep,  # noqa: ARG001 — auth gate; ownership check is the task_id itself
) -> TaskStatusResponse:
    """Return the Celery task status (+ result payload when ``SUCCESS``)."""
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
