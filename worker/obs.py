"""Worker observability foundation — structured JSON logs, correlation-id
propagation, per-stage ingest timings, and env-driven Sentry.

Phase 27. This module is intentionally **api-free**: it must never import
anything from ``api/`` (the dep-direction rule in the root ``CLAUDE.md`` —
``worker/`` has no upward deps). The only knowledge shared with the api
edge is the Celery message-header key string and the redaction deny-list,
which are **mirrored, not imported** (the ``tasks_client.RedisSettings`` /
``storage.sanitize_filename`` ↔ ``api/uploads.py`` precedent).

## Correlation id flow (lockstep with ``api/observability.py``)

``POST /upload`` mints (or echoes) an ``X-Correlation-ID`` and binds it
via ``structlog.contextvars``; ``api/tasks_client.enqueue_ingest`` forwards
it as a Celery message header keyed ``correlation_id``. Here, the
``task_prerun`` signal reads ``task.request.headers["correlation_id"]`` and
binds it (plus ``task_id`` + ``task_name``) so every ingest-stage log line
is greppable by the SAME id the api emitted at enqueue. A missing header
(eager mode, ``make enqueue``, manual CLI) degrades gracefully to a freshly
minted uuid — never a crash.

## Worker metrics surface

Infra has no pushgateway and the prefork child is short-lived, so the
worker runs no Prometheus server and pushes nothing. Ingest stage timings
are emitted as structured-log timings via ``log_stage`` — one JSON line per
stage carrying ``duration_ms`` + ``outcome`` + the bound correlation/task
ids. Queue depth IS visible in Prometheus because the api reads Redis LLEN
on scrape; that needs no worker cooperation.
"""

# Celery 5 ships without `py.typed`; its signal-handler decorators are
# loosely typed. Same relaxation pattern as celery_app.py / tasks/ingest.py.
# The signal handlers below are registered for their side effect by the
# `@task_prerun.connect` / `@worker_process_init.connect` decorators and are
# never referenced by name, so reportUnusedFunction is relaxed for this module.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUntypedFunctionDecorator=false, reportUnusedFunction=false

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import structlog
from celery.signals import task_postrun, task_prerun, worker_process_init
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Generator

    from sentry_sdk.types import Event, Hint
    from structlog.typing import EventDict, WrappedLogger

# --------------------------------------------------------------------------
# Mirrored constant — keep in lockstep with api/observability.py.
#
# This is the Celery MESSAGE-HEADER key the api enqueues the correlation id
# under (``api/tasks_client.enqueue_ingest`` -> send_task(headers={...})).
# The api's HTTP-header constant is ``CORRELATION_ID_HEADER = "X-Correlation-ID"``;
# the on-the-wire Celery key it forwards into is this exact string. If either
# side changes the literal, correlation silently breaks — change BOTH in the
# same PR (the tasks_client.RedisSettings mirror precedent).
# --------------------------------------------------------------------------
CELERY_CORRELATION_KEY = "correlation_id"

# --------------------------------------------------------------------------
# Mirrored deny-list — keep in lockstep with api/observability.py's copy.
#
# Case-insensitive key substrings whose values are scrubbed before a log
# line or Sentry event is rendered. ``x-correlation`` is deliberately NOT
# here — the correlation id is not sensitive and must stay greppable.
# Defence-in-depth only: the hard rule is that request bodies/headers, JWTs,
# and Redis/Postgres URLs (which embed passwords) are never passed to a log
# call in the first place (see the module docstring + AGENTS.md).
# --------------------------------------------------------------------------
_DENY_SUBSTRINGS: tuple[str, ...] = (
    "authorization",
    "token",  # also matches access_token / refresh_token
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "dsn",
    "jwt",
    "cookie",  # also matches set-cookie
    # Phase 44 — OAuth vault belt-and-suspenders, kept in lockstep with the
    # api/observability.py mirror. The first three are already caught by
    # ``token``/``secret``; ``code_verifier`` (the PKCE secret) is the one
    # genuinely-new addition (``code`` alone is too generic for the global
    # list). The worker never handles OAuth, but the two copies stay aligned.
    "refresh_token",
    "access_token",
    "code_verifier",
    "client_secret",
    # Redis/Postgres connection strings embed a password (celery_app.RedisSettings.url,
    # db settings); any *_url / *_uri / broker_url key is scrubbed so a stray
    # connection string can never leak its password into a log line. This is a
    # deliberate superset of the api mirror's must-redact set — keep both in
    # lockstep if either narrows it.
    "url",
    "uri",
)

_REDACTED = "[REDACTED]"


def _is_sensitive_key(key: str) -> bool:
    """True when *key* matches any deny-list substring (case-insensitive)."""
    lowered = key.lower()
    return any(sub in lowered for sub in _DENY_SUBSTRINGS)


def _redact(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor: replace any deny-listed key's value with ``[REDACTED]``.

    Mirror of ``api/observability.py:_redact``. Inserted into the processor
    chain BEFORE the JSON renderer, and reused as the Sentry ``before_send``
    scrubber, so neither a log line nor an event payload leaks a secret.
    """
    for key in list(event_dict.keys()):
        if _is_sensitive_key(str(key)):
            event_dict[key] = _REDACTED
    return event_dict


def _scrub_sentry_event(event: Event, _hint: Hint) -> Event:
    """Sentry ``before_send`` hook reusing the log deny-list.

    Walks the event's ``extra`` / ``tags`` shallow maps and redacts any
    deny-listed key. Belt-and-suspenders with ``send_default_pii=False``;
    the point is that even an accidental ``extra={"token": ...}`` is scrubbed
    before the event leaves the process.
    """
    for section in ("extra", "tags"):
        bag = event.get(section)
        if isinstance(bag, dict):
            for key in list(bag.keys()):
                if _is_sensitive_key(str(key)):
                    bag[key] = _REDACTED
    return event


_logging_configured = False


def configure_logging() -> None:
    """Render the stdlib root as one-line JSON via structlog. Idempotent.

    The worker's modules log through stdlib ``logging.getLogger(__name__)``
    (``ingest.py`` emits ``logger.warning(..., exc_info=...)``). Wiring the
    structlog ``ProcessorFormatter`` onto the stdlib root handler — with the
    deny-list redactor in the ``foreign_pre_chain`` — captures those existing
    call sites and JSON-renders + scrubs them WITHOUT touching any call site.
    Mirror of ``api/observability.py:configure_logging``.
    """
    global _logging_configured  # noqa: PLW0603 — module-level idempotency latch
    if _logging_configured:
        return

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        _redact,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # ``foreign_pre_chain`` runs on records produced by plain stdlib
        # logging (e.g. ingest.py's logger.warning) so they are redacted +
        # leveled + timestamped exactly like structlog-native records.
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    _logging_configured = True


def _mint_id() -> str:
    """Fresh correlation id for header-less enqueues (manual CLI / eager)."""
    return uuid.uuid4().hex


@task_prerun.connect
def _bind_correlation(  # noqa: ARG001 — Celery passes signal kwargs we don't all use
    task_id: str | None = None,
    task: Any = None,
    *_args: Any,
    **_kwargs: Any,
) -> None:
    """Bind the inbound correlation id (+ task id/name) for the task's logs.

    Reads the id from the Celery message header keyed ``CELERY_CORRELATION_KEY``
    (set by ``api/tasks_client.enqueue_ingest``); falls back to a minted id
    when the header is absent — eager mode, ``make enqueue``, or a manual CLI
    run never set it. Never raises out of the signal.
    """
    correlation_id = _mint_id()
    name = None
    if task is not None:
        request = getattr(task, "request", None)
        headers = getattr(request, "headers", None) or {}
        if isinstance(headers, dict):
            hdr = headers.get(CELERY_CORRELATION_KEY)
            if isinstance(hdr, str) and hdr:
                correlation_id = hdr
        name = getattr(task, "name", None)

    structlog.contextvars.bind_contextvars(
        correlation_id=correlation_id,
        task_id=task_id,
        task_name=name,
    )


@task_postrun.connect
def _clear_correlation(*_args: Any, **_kwargs: Any) -> None:
    """Clear the per-task context so ids never bleed across reused workers."""
    structlog.contextvars.clear_contextvars()


@contextmanager
def log_stage(stage: str, **fields: Any) -> Generator[None]:
    """Time one ingest stage and emit a single ``ingest_stage`` JSON line.

    The worker's metrics surface (no pushgateway — see module docstring):
    ``{event: "ingest_stage", stage, duration_ms, outcome, ...fields}``.
    ``correlation_id`` / ``task_id`` ride from the bound contextvars. On an
    exception the line is emitted with ``outcome="error"`` and the exception
    re-raised — the stage timing is observable even on failure.
    """
    start = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        duration_ms = round((time.perf_counter() - start) * 1000, 3)
        # Resolve the logger at call time (not import time) so it always picks
        # up the active structlog config — binding it at module import would
        # cache a stale logger under cache_logger_on_first_use.
        structlog.get_logger("worker.obs").info(
            "ingest_stage",
            stage=stage,
            duration_ms=duration_ms,
            outcome=outcome,
            **fields,
        )


class _SentrySettings(BaseSettings):
    """Worker Sentry config from ``SERMON_WORKER_*``.

    Separate object from ``celery_app.RedisSettings`` (which stays
    byte-identical to the api mirror) so the DSN never rides on the broker
    settings. Empty string -> ``None`` so compose's ``${VAR:-}`` delivers an
    unset DSN and Sentry stays off in dev.
    """

    model_config = SettingsConfigDict(env_prefix="SERMON_WORKER_", extra="ignore")

    sentry_dsn: str | None = None
    env: str = "dev"

    def resolved_dsn(self) -> str | None:
        dsn = self.sentry_dsn
        if dsn is not None and dsn.strip() == "":
            return None
        return dsn


def init_sentry() -> None:
    """Initialise Sentry only when ``SERMON_WORKER_SENTRY_DSN`` is set.

    No-op (zero network, total dev-default off) when the DSN is unset/empty.
    Wired per prefork child via ``worker_process_init`` so each fork inits.
    ``send_default_pii=False`` always; ``before_send`` reuses the log deny-list.
    Mirror of ``api/observability.py:init_sentry``.
    """
    cfg = _SentrySettings()
    dsn = cfg.resolved_dsn()
    if not dsn:
        return

    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration

    sentry_sdk.init(
        dsn=dsn,
        integrations=[CeleryIntegration()],
        send_default_pii=False,
        before_send=_scrub_sentry_event,
        environment=cfg.env,
        traces_sample_rate=0.0,
    )


@worker_process_init.connect
def _init_sentry_per_fork(*_args: Any, **_kwargs: Any) -> None:
    """Init Sentry inside each prefork child (its own process state)."""
    init_sentry()
