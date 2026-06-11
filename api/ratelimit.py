"""Redis-backed fixed-window rate limiting (Phase 19).

Hand-rolled over the already-locked ``redis.asyncio`` client instead of
slowapi/fastapi-limiter: the api is pyright-strict and slowapi's
decorator + ``Request``-coupled typing fights strict mode, the
fixed-window primitive is one atomic pipeline (INCR + first-hit EXPIRE),
and zero new dependencies keeps ``uv.lock`` unchanged. Full rationale +
the bucket table live in ``api/AGENTS.md``.

This is the SECOND limiting layer. Caddy already rate-limits per-IP at
the edge (``infra/caddy/Caddyfile`` zones ``auth``/``heavy``/``general``);
this layer adds what Caddy cannot see: cross-replica enforcement through
one shared Redis, per-USER granularity (Caddy has no JWT), and coverage
for traffic that never crosses Caddy (compose-network peers, the
host-published dev :8000).

Design notes:

- **Buckets** are named entries in ``_BUCKETS`` mapping to
  ``settings.ratelimit_<bucket>`` rate strings (``"<max requests>/<window
  seconds>"``). Adding one = a settings field + a ``_BUCKETS`` line + a
  route dependency (Phase 36 adds a generous autosave bucket this way).
  The getters are late-bound lambdas so env-driven limits and
  monkeypatched tests are honored at request time, never frozen at
  import.
- **Keys**: per-IP buckets key on :func:`client_ip` — the TCP peer by
  default, or the RIGHTMOST ``X-Forwarded-For`` entry when
  ``SERMON_API_TRUST_PROXY_HEADERS=true`` (rightmost = written by our own
  proxy hop, unforgeable by clients whether Caddy replaces inbound XFF
  (modern default) or appends to it; see :func:`client_ip`). Per-user
  buckets key on the JWT-derived ``user_id`` — behind the prod web proxy
  every browser shares ONE source IP, so per-IP keying there would let
  one user exhaust everyone.
- **Counters** live in logical db ``LIMITER_DB`` (2) of the broker Redis
  — db 0 is the Celery broker, db 1 the result backend
  (``tasks_client.RedisSettings``, the lockstep mirror of
  ``worker/celery_app.py``). A separate db keeps key-scan/FLUSHDB blast
  radius away from the broker; the mirror itself is deliberately NOT
  extended (db 2 is an api-only concern, see api/AGENTS.md).
- **Fail posture**: Redis-down fails OPEN with a loud log. This edge is
  abuse mitigation, not authorization — a Redis outage already takes
  /upload down (broker) and flips /readyz, and 429ing or 500ing logins
  because the limiter store blinked would turn an availability incident
  into a lockout. ``SERMON_API_RATELIMIT_ENABLED=false`` is the
  operational kill switch.
- **429 contract**: FastAPI-shaped ``{"detail": ...}`` JSON plus a
  ``Retry-After`` header (seconds), matching Caddy's edge 429s — the web
  proxies pass upstream status + detail through verbatim. The body never
  names Redis, the bucket, or the key (the Redis URL embeds a password).
- ``/healthz`` and ``/readyz`` are intentionally unlimited — compose
  HEALTHCHECK (Phase 29) and the k8s readinessProbe (Phase 30) poll them.
"""

# redis.asyncio command/pipeline methods return the `ResponseT =
# Awaitable | Any` union (same accommodation as readyz.py), which pyright
# strict reports as partially Unknown.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

import redis.asyncio as aioredis
from fastapi import HTTPException, Request, status

from settings import parse_rate, settings
from tasks_client import RedisSettings

logger = logging.getLogger(__name__)

# Logical Redis db for limiter counters. db 0 = Celery broker, db 1 =
# result backend (tasks_client.RedisSettings / worker/celery_app.py);
# db 2 is unused by every other component. See module docstring.
LIMITER_DB = 2

# Hard socket budget per limiter round trip. Small: the limiter sits in
# front of cheap routes (login/signup) where a wedged Redis must not add
# seconds of latency before the fail-open kicks in.
_OP_TIMEOUT_SECONDS = 1.0

# Named buckets → late-bound rate-string getters (see module docstring).
# Every name here must have a matching ``ratelimit_<name>`` field on
# ``ApiSettings`` (pinned by tests/test_ratelimit_unit.py).
_BUCKETS: dict[str, Callable[[], str]] = {
    "signup_ip": lambda: settings.ratelimit_signup_ip,
    "login_ip": lambda: settings.ratelimit_login_ip,
    "summary_user": lambda: settings.ratelimit_summary_user,
}

# Process-wide async client — lazy for the same reason as readyz.py's
# Milvus client: import time must never require reachable infra.
_redis_client: aioredis.Redis | None = None


def _redis() -> aioredis.Redis:
    global _redis_client  # noqa: PLW0603 — module-level singleton, see comment above
    if _redis_client is None:
        _redis_client = aioredis.Redis.from_url(
            RedisSettings().url(LIMITER_DB),
            socket_connect_timeout=_OP_TIMEOUT_SECONDS,
            socket_timeout=_OP_TIMEOUT_SECONDS,
        )
    return _redis_client


async def _hit(key: str, window: int) -> tuple[int, int]:
    """Record one hit on *key*; return ``(count_in_window, retry_after_s)``.

    One atomic MULTI/EXEC round trip: INCR, then EXPIRE NX (Redis ≥7;
    the stack pins ``redis:7-alpine``) so only the window's FIRST hit
    arms the TTL — later hits must not push the window forward — and a
    TTL read for the Retry-After value. Atomicity matters: a non-pipelined
    INCR-then-EXPIRE can crash in between and leave an immortal counter
    (a permanent 429 for that key).

    This is the module's test seam (the readyz ``_probe_*`` convention):
    unit tests monkeypatch ``ratelimit._hit`` instead of standing up Redis.
    """
    async with _redis().pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, window, nx=True)
        pipe.ttl(key)
        count, _, ttl = await pipe.execute()
    # TTL can read -1/-2 only in degenerate races; clamp so Retry-After
    # stays a sane positive integer.
    return int(count), max(int(ttl), 1)


def client_ip(request: Request) -> str:
    """Best-available client identity for per-IP buckets.

    Fail-closed default: the TCP peer address. Only when
    ``SERMON_API_TRUST_PROXY_HEADERS=true`` does ``X-Forwarded-For`` win —
    and then the RIGHTMOST entry, never the leftmost. Rationale: the
    rightmost hop is the one written by the proxy closest to us, the only
    part of the list a client cannot forge. Modern Caddy (≥2.5) replaces a
    client-supplied XFF outright (single, attested entry — rightmost ==
    leftmost), but if any hop ever APPENDS instead (older proxies, config
    drift), a client-prepended ``spoofed, real-ip`` list still keys on
    ``real-ip``. Leftmost parsing would let an attacker rotate the bucket
    per request. Our web proxy forwards Caddy's header verbatim and adds
    no hop of its own (web/lib/http.ts). Revisit only if a CDN/multi-hop
    chain lands in front of Caddy. With trust off, client-supplied XFF is
    ignored entirely.
    """
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    return request.client.host if request.client else "unknown"


async def enforce(bucket: str, identity: str) -> None:
    """Count one hit for *identity* in *bucket*; raise 429 past the limit.

    Reads the bucket's rate string at call time (env-driven, test-friendly),
    fails open with a loud log when Redis is unreachable (module docstring),
    and never leaks the key/bucket/Redis details into the 429 body.
    """
    if not settings.ratelimit_enabled:
        return
    limit, window = parse_rate(_BUCKETS[bucket]())
    key = f"ratelimit:{bucket}:{identity}"
    try:
        count, retry_after = await _hit(key, window)
    except Exception:  # noqa: BLE001 — deliberate fail-open; reason logged, never raised (see module docstring)
        logger.warning(
            "rate limiter store unreachable — failing OPEN (bucket=%s)",
            bucket,
            exc_info=True,
        )
        return
    if count > limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please try again shortly.",
            headers={"Retry-After": str(retry_after)},
        )


def ip_limit(bucket: str) -> Callable[[Request], Coroutine[Any, Any, None]]:
    """Route-decorator dependency factory: per-client-IP limit for *bucket*.

    Usage: ``dependencies=[Depends(ratelimit.ip_limit("login_ip"))]``.
    Unknown bucket names fail here — at import/wiring time — not on the
    first request. Per-USER buckets don't use this factory: they need the
    JWT identity, so the route module builds a small dependency on
    ``CurrentUserDep`` and calls :func:`enforce` itself (see summary.py;
    keeps this module free of an ``auth`` import cycle).
    """
    if bucket not in _BUCKETS:
        msg = f"Unknown rate-limit bucket {bucket!r}; register it in ratelimit._BUCKETS."
        raise KeyError(msg)

    async def dependency(request: Request) -> None:
        await enforce(bucket, client_ip(request))

    return dependency
