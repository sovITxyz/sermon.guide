"""Celery task wrapping the Phase 6/8 dedup-aware ingest pipeline.

Task signature: ``ingest_book(path, user_id)`` → ``dict`` with the
``IngestResult`` fields (UUIDs serialized as strings so the JSON
result-backend payload survives the Celery serializer).

This is intentionally a thin adapter — the synchronous pipeline in
``worker/ingest.py`` stays the source of truth. Reasons not to inline
the pipeline here:

- The CLI in ``ingest.py`` is still the manual debugging entrypoint.
- Tests cover ``ingest_markdown`` directly without spinning up Celery.
- Phase 10's API will likely import ``ingest_book.delay(...)`` *or*
  call the sync function directly in test setups; keeping one
  implementation prevents the two paths from drifting.

See ``celery_app.py`` for the broker config and the reliability
trade-offs the worker runs under (acks-late, requeue on worker loss,
visibility timeout 300s).
"""

# Celery 5 ships without `py.typed`; same relaxation pattern used in
# ingest.py / bootstrap_milvus.py.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUntypedFunctionDecorator=false

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from celery_app import app
from ingest import ingest


def _request_task_uuid(task: Any) -> UUID | None:
    """Parse ``task.request.id`` into the Phase 20 idempotency-claim key.

    Celery mints UUID4 string task ids by default (the api also mints
    them explicitly — ``api/tasks_client.py``), but the id is ultimately
    caller-controlled: an operator-supplied custom id or an eager test
    invocation may carry a non-UUID (or ``None``). Those runs fall back
    to ``None`` — the legacy claim-less posture — rather than failing.
    """
    raw = getattr(task.request, "id", None)
    if raw is None:
        return None
    try:
        return UUID(str(raw))
    except ValueError:
        return None


@app.task(name="tasks.ingest.ingest_book", bind=True)
def ingest_book(self: Any, path: str, user_id: str) -> dict[str, Any]:
    """Run the full ingest pipeline for one book.

    Both arguments are JSON-friendly strings so the broker payload
    matches Celery's default serializer (``json``):

    - *path*: absolute path to an EPUB/PDF visible to the worker
      process. R2/B2 object keys land in Phase 14+ — for now the worker
      reads the same local filesystem the API writes to.
    - *user_id*: UUID string of the owning ``users.user_id`` row.

    ``bind=True`` hands us the Task instance so ``self.request.id`` — the
    broker-stable task UUID, identical across redeliveries of the same
    message — can key the Phase 20 idempotency claim in ``upload_tasks``
    (see ``worker/ingest.py`` "Task-id claim").

    Returns a plain dict so the result backend can serialize it. The
    caller (Phase 10 API) maps it back into the typed ``IngestResult``
    on inspection.
    """
    result = ingest(
        path=Path(path),
        user_id=UUID(user_id),
        task_id=_request_task_uuid(self),
    )
    return {
        "book_id": str(result.book_id),
        "was_duplicate": result.was_duplicate,
        "rows_inserted": result.rows_inserted,
    }
