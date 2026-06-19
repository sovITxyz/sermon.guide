"""Celery application — Redis broker and result backend.

Phase 9 (ARCHITECTURE.md §2 ingestion runtime row, §5 upload path). The
single-process ingest CLI in ``worker/ingest.py`` stays callable for
manual runs; this module turns the same pipeline into a queueable task
so the API layer (Phase 10) can fan out uploads without blocking the
request.

## Broker URL

Read from ``SERMON_REDIS_*`` env. ``infra/.env`` carries the local-dev
defaults (host port ``63792`` because the dev box already runs Redis on
``6379``); production picks them up from k8s secrets. Two Redis DBs are
used so result inspection doesn't surface broker plumbing:

- ``db=0`` — broker (task queue)
- ``db=1`` — result backend

## Reliability config (worker crash semantics)

Spec calls for "kill mid-task → restart picks it up cleanly OR marks
failed". This module picks **restart picks it up cleanly**:

- ``task_acks_late=True`` — message is only acked after the task
  succeeds. A worker crash leaves the message in the queue.
- ``task_reject_on_worker_lost=True`` — when the prefork child dies
  (SIGKILL on a long-running ingest), the parent rejects with
  ``requeue=True`` so another worker picks it up.
- ``broker_transport_options.visibility_timeout=300`` — Redis-broker
  trick: an unacked message becomes redeliverable after 5 minutes if
  no worker is alive to reject it. Defaults to 1h, which makes the
  Phase-9 verify ("kill worker, watch restart resume") painful to
  observe interactively.
- ``worker_prefetch_multiplier=1`` — ingest tasks run for minutes
  (BGE-Large embeddings on CPU); prefetching N reservations behind a
  busy worker would block them on a single slow task.

**Idempotency (Phase 20).** The pipeline still writes Milvus before the
``global_books`` commit, but api-enqueued tasks now carry a
task-id-keyed claim in ``upload_tasks`` (``worker/ingest.py``
"Task-id claim"): the new-book path records its minted ``book_id`` on
the row before any non-transactional write, so a redelivery after a
mid-window crash scrubs the partial vectors and re-runs under the same
``book_id`` — one consistent record, zero orphans. Claim-less runs
(manual CLI / ``make enqueue``, no ``upload_tasks`` row) keep the old
Phase-9 posture: a mid-window crash orphans that attempt's vectors,
and only the MinHash dedup gate converges fully-committed re-uploads.
Residual for both: concurrent duplicate execution when the visibility
timeout expires under a still-running task (documented in ingest.py).
"""

# Celery 5 ships without `py.typed`; same relaxation pattern used in
# ingest.py / bootstrap_milvus.py.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false

from __future__ import annotations

from celery import Celery
from pydantic_settings import BaseSettings, SettingsConfigDict

import obs

# Phase 27 observability: this module is the Celery entrypoint loaded by
# `celery -A celery_app worker`, so importing obs here registers its
# task_prerun/postrun + worker_process_init signal handlers and configures
# structured JSON logging the moment the worker boots. configure_logging is
# idempotent; init_sentry is a no-op unless SERMON_WORKER_SENTRY_DSN is set
# (the per-fork worker_process_init signal also calls it). RedisSettings below
# stays byte-identical to the api mirror — the Sentry DSN lives on a SEPARATE
# settings object in obs.py, never on the broker settings.
obs.configure_logging()
obs.init_sentry()


class RedisSettings(BaseSettings):
    """Broker / backend connection settings from ``SERMON_REDIS_*``."""

    model_config = SettingsConfigDict(env_prefix="SERMON_REDIS_", extra="ignore")

    host: str = "localhost"
    port: int = 63792
    password: str = "sermon_local_dev"  # noqa: S105 — matches infra/.env local-dev default
    broker_db: int = 0
    backend_db: int = 1

    def url(self, db: int) -> str:
        """Compose ``redis://:<password>@<host>:<port>/<db>``."""
        return f"redis://:{self.password}@{self.host}:{self.port}/{db}"


settings = RedisSettings()


app = Celery(
    "sermon_worker",
    broker=settings.url(settings.broker_db),
    backend=settings.url(settings.backend_db),
    include=["tasks.ingest"],
)

app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    broker_transport_options={"visibility_timeout": 300},
    result_expires=3600,
    timezone="UTC",
    enable_utc=True,
)


if __name__ == "__main__":
    app.start()
