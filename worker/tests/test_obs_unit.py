"""Unit tests for the Phase 27 worker observability foundation (``obs.py``).

Keyless-fast — runs under ``make test`` with no infra, no key, no Redis.
Pins the four invariants the build plan calls out:

(a) Redaction deny-list scrubs every sensitive key — including a Redis
    broker URL whose password must NEVER survive (the readyz DSN-canary
    pattern from Phase 18).
(b) The ``task_prerun`` handler binds ``correlation_id`` from a fake
    ``request.headers`` and mints a fallback when the header is absent
    (manual / claim-less / eager enqueue); ``task_postrun`` clears it.
(c) ``log_stage`` emits exactly one ``ingest_stage`` line carrying
    ``stage`` + ``duration_ms`` and the bound ``correlation_id``.
(d) ``init_sentry`` is a strict no-op (never calls ``sentry_sdk.init``)
    when ``SERMON_WORKER_SENTRY_DSN`` is unset.
"""
# Tests reach into obs' module-private redaction processor + signal handlers
# (the test-seam access pattern test_dedup.py uses for Dedup internals); the
# autouse fixture is registered by pytest, not referenced by name.
# pyright: reportPrivateUsage=false, reportUnusedFunction=false

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
import structlog

import obs


@pytest.fixture(autouse=True)
def _clean_contextvars() -> Any:
    """Each test starts and ends with empty structlog contextvars."""
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


# --------------------------------------------------------------------------
# (a) redaction
# --------------------------------------------------------------------------

# A realistic Redis broker URL — the password segment is the canary that
# must never appear in rendered output (celery_app.RedisSettings.url shape).
_BROKER_URL = "redis://:sup3r_s3cret_pw@redis-host:63792/0"
_FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c3VwZXJzZWNyZXRzaWc"


def _render(event_dict: dict[str, Any]) -> str:
    """Run the deny-list processor then JSON-render, as the real chain does."""
    scrubbed = obs._redact(None, "info", dict(event_dict))
    rendered = structlog.processors.JSONRenderer()(None, "info", scrubbed)
    return rendered if isinstance(rendered, str) else rendered.decode()


def test_redact_scrubs_every_denied_key() -> None:
    payload: dict[str, Any] = {
        "authorization": "Bearer abc.def.ghi",
        "token": "raw-token-value",
        "access_token": "at-value",
        "refresh_token": "rt-value",
        "password": "hunter2",
        "passwd": "hunter3",
        "secret": "topsecret",
        "api_key": "ak-12345",
        "apikey": "ak-67890",
        "dsn": "https://pub@o0.ingest.sentry.io/1",
        "jwt": _FAKE_JWT,
        "cookie": "session=abc",
        "broker_dsn": _BROKER_URL,
        "event": "something happened",
        "correlation_id": "abc123",
    }
    rendered = _render(payload)
    parsed = json.loads(rendered)

    # Every sensitive value replaced; non-sensitive keys survive verbatim.
    for key in (
        "authorization",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "passwd",
        "secret",
        "api_key",
        "apikey",
        "dsn",
        "jwt",
        "cookie",
        "broker_dsn",
    ):
        assert parsed[key] == "[REDACTED]", key
    assert parsed["event"] == "something happened"
    assert parsed["correlation_id"] == "abc123"  # not sensitive — stays greppable


def test_redact_broker_password_never_survives() -> None:
    """The broker URL password must not leak in any form (DSN-canary)."""
    rendered = _render({"broker_url": _BROKER_URL, "event": "boot"})
    assert "sup3r_s3cret_pw" not in rendered
    assert _BROKER_URL not in rendered


def test_redact_jwt_never_survives() -> None:
    rendered = _render({"jwt_token": _FAKE_JWT, "event": "auth"})
    assert _FAKE_JWT not in rendered


# --------------------------------------------------------------------------
# (b) correlation-id binding via task_prerun / clearing via task_postrun
# --------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, headers: dict[str, Any]) -> None:
        self.headers = headers
        self.id = "req-id-ignored"


class _FakeTask:
    def __init__(self, *, name: str, headers: dict[str, Any]) -> None:
        self.name = name
        self.request = _FakeRequest(headers)


def test_prerun_binds_correlation_from_header() -> None:
    task = _FakeTask(
        name="tasks.ingest.ingest_book",
        headers={obs.CELERY_CORRELATION_KEY: "deadbeef"},
    )
    obs._bind_correlation(task_id="task-7", task=task)

    bound = structlog.contextvars.get_contextvars()
    assert bound["correlation_id"] == "deadbeef"
    assert bound["task_id"] == "task-7"
    assert bound["task_name"] == "tasks.ingest.ingest_book"


def test_prerun_mints_fallback_when_header_absent() -> None:
    task = _FakeTask(name="tasks.ingest.ingest_book", headers={})
    obs._bind_correlation(task_id="task-9", task=task)

    bound = structlog.contextvars.get_contextvars()
    cid = bound["correlation_id"]
    assert isinstance(cid, str)
    assert len(cid) == 32  # uuid4().hex — a minted fallback, never empty
    assert bound["task_id"] == "task-9"


def test_prerun_mints_fallback_when_header_garbage() -> None:
    """A non-string / empty header falls back to a minted id, never crashes."""
    task = _FakeTask(name="t", headers={obs.CELERY_CORRELATION_KEY: ""})
    obs._bind_correlation(task_id="task-x", task=task)
    cid = structlog.contextvars.get_contextvars()["correlation_id"]
    assert len(cid) == 32


def test_postrun_clears_contextvars() -> None:
    structlog.contextvars.bind_contextvars(correlation_id="abc", task_id="t1")
    assert structlog.contextvars.get_contextvars() != {}
    obs._clear_correlation()
    assert structlog.contextvars.get_contextvars() == {}


# --------------------------------------------------------------------------
# (c) log_stage emits one ingest_stage line carrying the bound id
# --------------------------------------------------------------------------


def test_log_stage_emits_one_line_with_duration_and_correlation() -> None:
    cap = structlog.testing.LogCapture()
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, cap],
        wrapper_class=structlog.BoundLogger,
    )
    try:
        structlog.contextvars.bind_contextvars(correlation_id="trace-42", task_id="t-99")
        with obs.log_stage("embed", book_id="b-1"):
            pass
    finally:
        structlog.reset_defaults()

    lines = [e for e in cap.entries if e.get("event") == "ingest_stage"]
    assert len(lines) == 1
    entry = lines[0]
    assert entry["stage"] == "embed"
    assert entry["outcome"] == "ok"
    assert isinstance(entry["duration_ms"], float)
    assert entry["book_id"] == "b-1"
    assert entry["correlation_id"] == "trace-42"
    assert entry["task_id"] == "t-99"


def test_log_stage_emits_error_outcome_and_reraises() -> None:
    cap = structlog.testing.LogCapture()
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, cap],
        wrapper_class=structlog.BoundLogger,
    )
    try:
        with pytest.raises(ValueError, match="boom"), obs.log_stage("chunk"):
            raise ValueError("boom")
    finally:
        structlog.reset_defaults()

    lines = [e for e in cap.entries if e.get("event") == "ingest_stage"]
    assert len(lines) == 1
    assert lines[0]["stage"] == "chunk"
    assert lines[0]["outcome"] == "error"


# --------------------------------------------------------------------------
# (d) init_sentry is a no-op when the DSN is unset / empty
# --------------------------------------------------------------------------


def test_init_sentry_noop_when_dsn_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SERMON_WORKER_SENTRY_DSN", raising=False)
    import sentry_sdk

    called = False

    def _boom(*_a: Any, **_k: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(sentry_sdk, "init", _boom)
    obs.init_sentry()
    assert called is False


def test_init_sentry_noop_when_dsn_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERMON_WORKER_SENTRY_DSN", "")
    import sentry_sdk

    called = False

    def _boom(*_a: Any, **_k: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(sentry_sdk, "init", _boom)
    obs.init_sentry()
    assert called is False


# --------------------------------------------------------------------------
# Cross-package mirror guard — keep the Celery header key in lockstep with
# api/observability.py (the tasks_client.RedisSettings mirror precedent).
# --------------------------------------------------------------------------


def test_celery_correlation_key_literal_is_pinned() -> None:
    # If this literal changes, api/observability.py's mirror must change too,
    # or the api enqueues a header the worker never reads.
    assert obs.CELERY_CORRELATION_KEY == "correlation_id"


# --------------------------------------------------------------------------
# configure_logging idempotency + stdlib bridge (existing logger.warning
# call sites must render as redacted JSON, not bypass the chain).
# --------------------------------------------------------------------------


def test_configure_logging_is_idempotent() -> None:
    # Snapshot, configure twice, assert one handler — then restore so we
    # don't leave a JSON formatter bolted onto the root for sibling tests.
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        obs._logging_configured = False
        obs.configure_logging()
        n_after_first = len(root.handlers)
        obs.configure_logging()  # idempotent: second call is a no-op
        assert len(root.handlers) == n_after_first == 1
    finally:
        obs._logging_configured = False
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        structlog.reset_defaults()
