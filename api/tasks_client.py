"""Celery client — broker URL only, no worker deps imported.

The api/ process never runs the ingest pipeline; it only *enqueues* by
task name (``tasks.ingest.ingest_book``) so the api venv stays free of
the extractor stack (pandoc / EbookLib / pymupdf4llm) — worker concerns.
A separate ``Celery()`` instance against the same Redis broker + result
backend is enough for ``send_task`` + ``AsyncResult``.

Connection settings mirror ``worker/celery_app.py`` exactly: same env
prefix (``SERMON_REDIS_*``), same broker/backend db split (0/1), same
password handling. Drift here means the api enqueues into a different
queue than the worker reads — silent failure mode.
"""

# Celery 5 ships without `py.typed`; mirror worker/celery_app.py's relaxation.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false

from __future__ import annotations

import uuid

import structlog
from celery import Celery
from celery.result import AsyncResult
from pydantic_settings import BaseSettings, SettingsConfigDict

from observability import CELERY_CORRELATION_KEY

INGEST_TASK_NAME = "tasks.ingest.ingest_book"


class RedisSettings(BaseSettings):
    """Mirror of ``worker/celery_app.py:RedisSettings`` — keep in lockstep."""

    model_config = SettingsConfigDict(env_prefix="SERMON_REDIS_", extra="ignore")

    host: str = "localhost"
    port: int = 63792
    password: str = "sermon_local_dev"  # noqa: S105 — matches infra/.env local-dev default
    broker_db: int = 0
    backend_db: int = 1

    def url(self, db: int) -> str:
        return f"redis://:{self.password}@{self.host}:{self.port}/{db}"


_redis = RedisSettings()

# Name has to match worker/celery_app.py so the result backend keys line up
# (Celery namespaces results by app name).
celery_client = Celery(
    "sermon_worker",
    broker=_redis.url(_redis.broker_db),
    backend=_redis.url(_redis.backend_db),
)

celery_client.conf.update(
    timezone="UTC",
    enable_utc=True,
)


def enqueue_ingest(*, path: str, user_id: str, task_id: str) -> str:
    """Enqueue a single ingest task and return the Celery ``task_id``.

    Strings are passed by name to match the worker's task signature
    (``tasks.ingest.ingest_book(path, user_id)``). Both are
    JSON-serializable — Celery's default serializer is ``json``.

    *task_id* is REQUIRED and minted by the caller (Phase 20): the
    ``/upload`` route commits the ``upload_tasks`` ownership/idempotency
    row under that UUID *before* this call, so the id must exist before
    Celery sees the message. Letting Celery mint it here would reopen
    the crash window where a task runs without an owner row.
    """
    # Phase 27: propagate the request's correlation id into the Celery message
    # headers so the worker's ``task_prerun`` signal (``worker/obs.py``) can
    # bind it and every ingest-stage log line is greppable by the same id the
    # HTTP request echoed. Read from the structlog contextvars the correlation
    # middleware bound; a non-HTTP caller (a CLI ``make enqueue``, a manual
    # run) has nothing bound, so mint a fresh uuid — the worker side ALSO falls
    # back to a minted id, so a dropped header is never fatal. The task
    # SIGNATURE (``ingest_book(path, user_id)``) is unchanged: this is a
    # transport header only.
    bound = structlog.contextvars.get_contextvars().get("correlation_id")
    correlation_id = bound if isinstance(bound, str) and bound else uuid.uuid4().hex
    async_result = celery_client.send_task(
        INGEST_TASK_NAME,
        args=[path, user_id],
        task_id=task_id,
        headers={CELERY_CORRELATION_KEY: correlation_id},
    )
    return async_result.id


def task_status(task_id: str) -> AsyncResult:
    """Return the ``AsyncResult`` for *task_id* against the shared backend."""
    return AsyncResult(task_id, app=celery_client)
