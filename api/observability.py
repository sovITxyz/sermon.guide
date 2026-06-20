"""API logging + correlation-id + Sentry foundation (Phase 27).

This is the api half of a deliberately MIRRORED (not imported) pair with
``worker/obs.py``. The dep-direction rule (root ``CLAUDE.md``) forbids
``api`` ↔ ``worker`` cross-imports beyond the six sanctioned ``worker`` symbols,
so the correlation header/key strings and the redaction deny-list each have one
copy here and one copy in ``worker/obs.py``, each pointing at the other — the
same mirror-not-import precedent as ``tasks_client.RedisSettings`` /
``uploads._ALLOWED_UPLOAD_MIMES``. Propagation rides Celery MESSAGE HEADERS
(``send_task(headers=...)`` on this side; ``task.request.headers`` on the
worker side), so neither the ingest task signature nor any import boundary
changes.

What lives here:

1. ``CORRELATION_ID_HEADER`` (HTTP) + ``CELERY_CORRELATION_KEY`` (Celery
   message header) — the lockstep-mirror constants.
2. ``configure_logging()`` — wires structlog as a ``ProcessorFormatter`` on the
   stdlib root so EVERY ``logging.getLogger(__name__)`` call (the codebase
   already logs that way: ``search.py``/``readyz.py``/``ratelimit.py`` emit via
   stdlib ``logger.warning(..., exc_info=...)``) renders as redacted one-line
   JSON — without touching a single call site. Idempotent.
3. ``redact_event`` — the deny-list scrubber, mirror of ``worker/obs.py``,
   inserted into the processor chain BEFORE ``JSONRenderer`` AND reused as the
   Sentry ``before_send`` hook.
4. ``correlation_middleware`` — reads/​mints the correlation id, binds it via
   ``structlog.contextvars`` (so every log line on the request carries it),
   times the request into ``REQUEST_DURATION`` keyed on the matched route
   TEMPLATE (never the raw path — cardinality), echoes the id on the response,
   and clears contextvars in ``finally``. NEVER logs the body or headers.
5. ``init_sentry()`` — no-op when no DSN (dev default → zero network); else a
   scrubbed, PII-free init.
"""

# structlog ships py.typed, but the ProcessorFormatter foreign_pre_chain and
# the contextvars helpers expose a few loosely-typed Any seams; relax the
# Unknown* reports locally rather than weakening global strictness (the
# celery_app.py / tasks_client.py precedent).
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from metrics import REQUEST_DURATION
from settings import settings

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from sentry_sdk._types import Event, Hint

# ---------------------------------------------------------------------------
# Lockstep-mirror constants. The lockstep mirror of these lives in
# ``worker/obs.py`` (``CELERY_CORRELATION_KEY``). If the header/key string
# changes on one side, correlation silently breaks — the api would enqueue a
# header the worker never reads. Keep both copies identical (a cross-package
# equality assertion guards against drift; tasks_client.RedisSettings
# precedent).

#: Inbound/outbound HTTP correlation header. Read case-insensitively (Starlette
#: lowercases header keys); echoed verbatim on the response.
CORRELATION_ID_HEADER = "X-Correlation-ID"

#: Celery message-header key carrying the correlation id from the api enqueue
#: into the worker's ``task_prerun`` handler. MUST equal
#: ``worker/obs.py:CELERY_CORRELATION_KEY``.
CELERY_CORRELATION_KEY = "correlation_id"

# ---------------------------------------------------------------------------
# Redaction (mirror of worker/obs.py:redact_event).

# Case-insensitive key substrings whose VALUES must never reach a log line or a
# Sentry payload. ``x-correlation`` is deliberately NOT here — the correlation
# id is not sensitive and we want it on every line. Substring match catches
# ``access_token``/``refresh_token`` via ``token``, ``apikey``/``api_key`` via
# both spellings, the Redis/Postgres ``dsn``/url password via ``dsn`` (and the
# explicit url/cookie keys).
_DENY_SUBSTRINGS = (
    "authorization",
    "token",
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "dsn",
    "jwt",
    "cookie",
    "set-cookie",
    # Phase 44 — OAuth vault belt-and-suspenders. The first three are already
    # caught by ``token``/``secret``; spelling them out is documentation-as-code
    # (and a guard if ``token`` is ever narrowed). ``code_verifier`` is the one
    # genuinely-new addition — the PKCE secret is not caught by any substring
    # above (``code`` alone is too generic for the global list — it would redact
    # status_code/error_code). Keep BYTE-IDENTICAL with worker/obs.py.
    "refresh_token",
    "access_token",
    "code_verifier",
    "client_secret",
)

_REDACTED = "[REDACTED]"


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(needle in lowered for needle in _DENY_SUBSTRINGS)


def redact_event(
    _logger: object,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """structlog processor: replace any deny-listed key's value with ``[REDACTED]``.

    Mirror of ``worker/obs.py:redact_event``. Inserted into the chain BEFORE
    ``JSONRenderer`` so the rendered line never carries a token/secret/DSN, and
    reused (via ``_sentry_before_send``) as the Sentry ``before_send`` scrubber.
    Deny-list is key-substring based: the hard rule that bodies/headers/tokens
    are never PASSED to a log call in the first place is the primary defense
    (a secret interpolated into a message string would not be caught by key
    matching) — this is belt-and-suspenders.
    """
    for key in list(event_dict.keys()):
        if _is_sensitive_key(key):
            event_dict[key] = _REDACTED
    return event_dict


# ---------------------------------------------------------------------------
# Logging configuration.

_TIMESTAMPER = structlog.processors.TimeStamper(fmt="iso", utc=True)

# Processors shared by both the structlog-native path and the stdlib
# ``foreign_pre_chain`` (so ``logger.warning(...)`` calls from existing modules
# get the same level/timestamp/redaction treatment before rendering).
_SHARED_PROCESSORS: tuple[Any, ...] = (
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    _TIMESTAMPER,
    redact_event,
)

_configured = False


def configure_logging() -> None:
    """Attach the structlog JSON ProcessorFormatter to the stdlib root. Idempotent.

    Existing modules log via stdlib ``logging.getLogger(__name__)`` with
    ``exc_info=...``; wiring structlog as a ``ProcessorFormatter`` on the root
    handler (with a ``foreign_pre_chain``) means those calls render as redacted
    one-line JSON without any call-site change — and crucially get REDACTED too
    (a structlog-only config would let the existing ``logger.warning`` lines
    bypass the deny-list, a leak risk pinned by the stdlib-bridge test).
    """
    global _configured  # noqa: PLW0603 — module-level idempotency guard
    if _configured:
        return

    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            # Hand the event dict to the stdlib formatter, which finishes the
            # render with the JSONRenderer below.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # ``foreign_pre_chain`` runs on records that did NOT originate from a
        # structlog logger (every existing ``logging.getLogger`` call) — so
        # they get the level/timestamp/redaction treatment before the shared
        # renderer. The exc_info traceback text is rendered by
        # ``format_exc_info`` and then JSON-encoded; the deny-list above
        # scrubs structured keys (DSNs are kept out of message strings by the
        # never-interpolate-secrets rule).
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            _TIMESTAMPER,
            # ``ExtraAdder`` MUST run BEFORE ``redact_event`` so a stdlib
            # ``logger.warning(..., extra={"dsn": ...})`` has its extra keys
            # merged into the event dict in time to be scrubbed — otherwise a
            # secret in ``extra`` bypasses the deny-list (the leak-risk pin).
            structlog.stdlib.ExtraAdder(),
            redact_event,
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    # Replace any pre-existing handlers so we don't double-emit (plain + JSON).
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    _configured = True


# ---------------------------------------------------------------------------
# Correlation + latency middleware.


def _inbound_correlation_id(scope: Scope) -> str:
    """Read a sane inbound ``X-Correlation-ID``, or mint a fresh ``uuid4().hex``.

    Garbage/absent → a minted id (we never echo back attacker-shaped header
    junk unbounded). Accepts any non-empty, reasonably-bounded token so a real
    upstream id (e.g. a trace id) propagates; otherwise mints.
    """
    headers = scope.get("headers") or []
    wanted = CORRELATION_ID_HEADER.lower().encode("latin-1")
    for raw_key, raw_value in headers:
        if raw_key.lower() == wanted:
            value = raw_value.decode("latin-1", "replace").strip()
            # Bound the accepted id so a pathological header can't bloat every
            # log line / response header. printable, no control chars.
            if value and len(value) <= 200 and value.isprintable():
                return value
    return uuid.uuid4().hex


def _route_template(scope: Scope) -> str:
    """The matched APIRoute path TEMPLATE (``/tasks/{task_id}``), never the raw path.

    Labelling the latency histogram by the raw path would turn every UUID into
    a label value and explode Prometheus memory (the Phase 27 cardinality
    risk). When no route matched (404), fall back to a single ``__unmatched__``
    bucket rather than the raw path.
    """
    route = scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template
    return "__unmatched__"


class CorrelationMiddleware:
    """ASGI middleware: bind a correlation id + time every request into Prometheus.

    Pure-ASGI (not ``BaseHTTPMiddleware``) so it sees the matched ``route`` in
    ``scope`` after routing — and so it is cheap and exception-transparent. It
    is added OUTERMOST in ``main.py`` so even CORS-rejected / 4xx responses get
    an id and are timed.

    NEVER logs the request body or headers — it only reads the correlation
    header BY NAME and binds the id. The HTTP request log line (if any) carries
    method, route template, status, and duration only.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        correlation_id = _inbound_correlation_id(scope)
        method = scope.get("method", "GET")
        start = time.perf_counter()
        status_code = 500
        encoded_header = (
            CORRELATION_ID_HEADER.encode("latin-1"),
            correlation_id.encode("latin-1"),
        )

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status", 500))
                # Echo the correlation id back so the caller (and the worker,
                # via the enqueue header) can grep one id end-to-end.
                raw_headers = list(message.get("headers") or [])
                raw_headers.append(encoded_header)
                message = {**message, "headers": raw_headers}
            await send(message)

        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - start
            REQUEST_DURATION.labels(
                route=_route_template(scope),
                method=method,
                status=str(status_code),
            ).observe(duration)
            structlog.contextvars.clear_contextvars()


# ---------------------------------------------------------------------------
# Sentry.


def _sentry_before_send(event: Event, _hint: Hint) -> Event | None:
    """Scrub a Sentry event payload with the SAME deny-list as the log redactor.

    Belt-and-suspenders with ``send_default_pii=False``: any deny-listed top-level
    key in the event dict has its value replaced. We do not deep-walk the whole
    nested structure (Sentry's own ``send_default_pii=False`` already drops
    request bodies/headers/cookies) — this guards against an accidentally-attached
    deny-listed top-level extra.
    """
    # ``Event`` is a TypedDict, not a plain MutableMapping, so apply the same
    # deny-list directly rather than reusing ``redact_event``'s signature.
    for key in list(event.keys()):
        if _is_sensitive_key(key):
            event[key] = _REDACTED  # type: ignore[literal-required]  # dynamic deny-list key
    return event


def init_sentry() -> None:
    """Initialize Sentry iff a DSN is configured; otherwise a total no-op.

    Dev default: ``SERMON_API_SENTRY_DSN`` is unset/empty → ``sentry_sdk.init``
    is NOT called → zero network. ``send_default_pii=False`` always (with the
    ``before_send`` scrubber as a second layer); ``traces_sample_rate`` defaults
    to 0. Wired from ``main.py``'s lifespan.
    """
    dsn = settings.sentry_dsn
    if not dsn:
        return

    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    sentry_sdk.init(
        dsn=dsn,
        integrations=[StarletteIntegration(), FastApiIntegration()],
        send_default_pii=False,
        before_send=_sentry_before_send,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        environment=settings.env,
    )
