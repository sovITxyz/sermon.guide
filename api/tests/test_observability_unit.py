"""Unit tests for the Phase 27 observability foundation.

Covers:
(a) the correlation middleware — inbound ``X-Correlation-ID`` is echoed and
    bound; absent → a uuid is minted and echoed; contextvars are cleared after
    the request (and a 4xx still carries an id — the outermost-middleware
    contract);
(b) the redaction processor — a dict carrying every deny-listed key + a fake
    Redis URL / JWT renders with every sensitive value ``[REDACTED]`` and the
    raw secret/DSN ABSENT from the JSON (the readyz DSN-canary pattern), AND
    the same scrubbing runs through the stdlib-logging bridge (so existing
    ``logger.warning`` calls can't leak);
(c) the HTTP request path never logs a body;
(d) ``tasks_client.enqueue_ingest`` forwards the bound correlation id in the
    ``send_task`` headers (monkeypatched ``send_task``), and mints one when no
    id is bound;
(e) the cross-package mirror guard: the api ``CELERY_CORRELATION_KEY`` matches
    the worker's literal ``"correlation_id"``.
"""

# Tests exercise module-internals on purpose.
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient

import main as main_module
import observability
import tasks_client
from observability import CORRELATION_ID_HEADER, redact_event
from settings import DEV_JWT_SECRET

_STAGED_PATH = "/staged/x.epub"


@pytest.fixture
def dev_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A ``TestClient`` booted in dev posture (the boot guards pass).

    ``with TestClient(app)`` runs the lifespan exactly like uvicorn — dev
    posture + the placeholder secret boots cleanly (mirrors
    ``test_main_unit.py``'s convention).
    """
    monkeypatch.setattr(main_module.settings, "env", "dev")
    monkeypatch.setattr(main_module.settings, "jwt_secret", DEV_JWT_SECRET)
    monkeypatch.setattr(main_module.settings, "cors_origins", ["http://localhost:3000"])
    with TestClient(main_module.app) as client:
        yield client


# --- (a) correlation middleware ---------------------------------------------


def test_inbound_correlation_id_is_echoed(dev_client: Any) -> None:
    response = dev_client.get("/healthz", headers={CORRELATION_ID_HEADER: "abc-123-def"})
    assert response.status_code == 200
    assert response.headers[CORRELATION_ID_HEADER] == "abc-123-def"


def test_absent_correlation_id_is_minted_and_echoed(dev_client: Any) -> None:
    response = dev_client.get("/healthz")
    assert response.status_code == 200
    minted = response.headers.get(CORRELATION_ID_HEADER)
    assert minted
    # A minted id is a uuid4 hex (32 hex chars), never empty.
    assert len(minted) == 32
    int(minted, 16)  # parses as hex — raises if it didn't mint a uuid


def test_garbage_correlation_id_is_replaced_with_a_minted_one(dev_client: Any) -> None:
    # A control-char-laden / over-long header is not echoed verbatim.
    response = dev_client.get("/healthz", headers={CORRELATION_ID_HEADER: "x" * 500})
    echoed = response.headers.get(CORRELATION_ID_HEADER)
    assert echoed
    assert echoed != "x" * 500
    assert len(echoed) == 32


def test_4xx_response_still_carries_a_correlation_id(dev_client: Any) -> None:
    """The middleware is OUTERMOST, so even an unmatched-route 404 gets an id."""
    response = dev_client.get("/this-route-does-not-exist")
    assert response.status_code == 404
    assert response.headers.get(CORRELATION_ID_HEADER)


def test_contextvars_cleared_after_request(dev_client: Any) -> None:
    dev_client.get("/healthz", headers={CORRELATION_ID_HEADER: "leak-check"})
    # The finally in the middleware must have cleared the binding — no leak
    # into the next request / the test thread.
    assert "correlation_id" not in structlog.contextvars.get_contextvars()


# --- (b) redaction processor ------------------------------------------------

_FAKE_REDIS_URL = "redis://:sermon_local_dev@localhost:63792/0"  # noqa: S105
_FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.SECRETSIG"  # noqa: S105


def test_redact_event_replaces_every_denied_key() -> None:
    event = {
        "event": "something happened",
        "authorization": "Bearer sk-live-XYZ",
        "token": "tok_abc",
        "access_token": "at_abc",
        "refresh_token": "rt_abc",
        "password": "hunter2",
        "passwd": "hunter2",
        "secret": "s3cr3t",
        "api_key": "ak_abc",
        "apikey": "ak_abc",
        "dsn": "https://abc@sentry.example/1",
        "jwt": _FAKE_JWT,
        "cookie": "session=abc",
        "redis_dsn": _FAKE_REDIS_URL,
        "correlation_id": "keep-me",
    }
    out = redact_event(None, "info", dict(event))
    rendered = json.dumps(out)

    # Every sensitive value is gone; the correlation id and event survive.
    assert out["correlation_id"] == "keep-me"
    assert out["event"] == "something happened"
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
        "redis_dsn",
    ):
        assert out[key] == "[REDACTED]", f"{key} not redacted"

    # The raw secret/DSN material never survives into the rendered JSON.
    assert "hunter2" not in rendered
    assert "sermon_local_dev" not in rendered
    assert "SECRETSIG" not in rendered
    assert "sk-live-XYZ" not in rendered


def test_stdlib_logging_bridge_is_redacted() -> None:
    """Existing ``logging.getLogger`` calls render as JSON AND get redacted.

    This is the leak-risk pin from the build plan: if only structlog were
    configured, the existing ``logger.warning(..., exc_info=...)`` lines would
    bypass the deny-list. ``configure_logging`` attaches the ProcessorFormatter
    to the stdlib ROOT, so a stdlib logger's ``extra`` dict is scrubbed too.

    Rendered through the configured root handler's ProcessorFormatter directly
    (a real ``LogRecord``) rather than via stream capture: the handler binds
    the original ``sys.stderr`` at construction, which pytest's ``capsys`` does
    not see — formatting the record is the faithful, capture-independent check.
    """
    observability.configure_logging()
    root_handler = logging.getLogger().handlers[0]
    record = logging.LogRecord(
        name="test.bridge",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="boom while connecting",
        args=(),
        exc_info=None,
    )
    # The ``extra=`` dict on a real ``logger.warning`` lands as record
    # attributes; replicate that so ExtraAdder picks them up.
    record.dsn = _FAKE_REDIS_URL
    record.password = "hunter2"  # noqa: S105
    rendered = root_handler.format(record)

    assert "sermon_local_dev" not in rendered
    assert "hunter2" not in rendered
    assert '"dsn": "[REDACTED]"' in rendered
    assert '"password": "[REDACTED]"' in rendered


# --- (c) the HTTP request path never logs a body ----------------------------


def test_request_body_is_never_logged(
    dev_client: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_body = "TOP-SECRET-BODY-MARKER-12345"
    # /healthz ignores the body, but the middleware must never log it either.
    dev_client.post("/healthz", content=secret_body)
    stream = capsys.readouterr()
    assert secret_body not in (stream.err + stream.out)


# --- (d) enqueue_ingest forwards the bound correlation id -------------------


class _FakeAsyncResult:
    id = "task-123"


def test_enqueue_forwards_bound_correlation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_send_task(name: str, **kwargs: Any) -> _FakeAsyncResult:
        captured["name"] = name
        captured["kwargs"] = kwargs
        return _FakeAsyncResult()

    monkeypatch.setattr(tasks_client.celery_client, "send_task", _fake_send_task)

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(correlation_id="req-corr-id-xyz")
    try:
        tasks_client.enqueue_ingest(path=_STAGED_PATH, user_id="u1", task_id="t1")
    finally:
        structlog.contextvars.clear_contextvars()

    headers = captured["kwargs"]["headers"]
    assert headers[tasks_client.CELERY_CORRELATION_KEY] == "req-corr-id-xyz"
    # Task signature unchanged — args are still [path, user_id], task_id pinned.
    assert captured["kwargs"]["args"] == [_STAGED_PATH, "u1"]
    assert captured["kwargs"]["task_id"] == "t1"


def test_enqueue_mints_id_when_unbound(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-HTTP caller (no bound id) still ships a header — minted uuid."""
    captured: dict[str, Any] = {}

    def _fake_send_task(_name: str, **kwargs: Any) -> _FakeAsyncResult:
        captured["kwargs"] = kwargs
        return _FakeAsyncResult()

    monkeypatch.setattr(tasks_client.celery_client, "send_task", _fake_send_task)
    structlog.contextvars.clear_contextvars()

    tasks_client.enqueue_ingest(path=_STAGED_PATH, user_id="u1", task_id="t1")
    minted = captured["kwargs"]["headers"][tasks_client.CELERY_CORRELATION_KEY]
    assert minted
    assert len(minted) == 32  # uuid4().hex


# --- (e) cross-package mirror guard -----------------------------------------


def test_correlation_key_matches_worker_mirror() -> None:
    """The api CELERY_CORRELATION_KEY must equal the worker's literal mirror.

    Mirror-not-import (dep-direction rule): the two copies must not drift, or
    the api enqueues a header the worker never reads. The worker side defines
    the SAME literal ``"correlation_id"`` in ``worker/obs.py``.
    """
    assert observability.CELERY_CORRELATION_KEY == "correlation_id"
    assert tasks_client.CELERY_CORRELATION_KEY == "correlation_id"
