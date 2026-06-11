"""Unit tests for the Phase 19 Redis-backed rate limiter (``ratelimit.py``).

No live Redis ever (suite convention): ``ratelimit._hit`` is the module's
designated test seam — the same pattern as ``readyz._probe_*`` — so every
test swaps it for an in-memory counter. Settings are monkeypatched as
ATTRIBUTES on the shared singleton (the ``test_main_unit.py`` convention);
the rate-string *parsing* layer is covered in ``test_settings_unit.py``.

Wiring tests go through ``TestClient`` against the real app WITHOUT the
lifespan (bare client = no boot guards), proving the dependencies sit on
the real routes and fire before handlers — the 429 path must never need a
database, an LLM key, or Redis.
"""

# Tests exercise module-internals on purpose.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from db import User
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

import auth
import main as main_module
import ratelimit
from settings import parse_rate, settings

# ---------------------------------------------------------------------------
# helpers


class _FakeStore:
    """In-memory stand-in for the ``_hit`` Redis seam."""

    def __init__(self, ttl: int = 30) -> None:
        self.counts: dict[str, int] = {}
        self.windows: dict[str, int] = {}
        self.ttl = ttl

    async def hit(self, key: str, window: int) -> tuple[int, int]:
        self.counts[key] = self.counts.get(key, 0) + 1
        self.windows[key] = window
        return self.counts[key], self.ttl


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    fake = _FakeStore()
    monkeypatch.setattr(ratelimit, "_hit", fake.hit)
    return fake


def _request(
    headers: dict[str, str] | None = None,
    client_host: str | None = "10.0.0.7",
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": (client_host, 1234) if client_host is not None else None,
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# counting + 429 behavior


async def test_enforce_allows_under_the_limit(store: _FakeStore) -> None:
    for _ in range(10):  # default login_ip bucket is 10/60
        await ratelimit.enforce("login_ip", "1.2.3.4")
    assert store.counts["ratelimit:login_ip:1.2.3.4"] == 10


async def test_enforce_429_past_the_limit_with_retry_after(store: _FakeStore) -> None:
    for _ in range(10):
        await ratelimit.enforce("login_ip", "1.2.3.4")
    with pytest.raises(HTTPException) as excinfo:
        await ratelimit.enforce("login_ip", "1.2.3.4")
    assert excinfo.value.status_code == 429
    assert excinfo.value.headers is not None
    assert excinfo.value.headers["Retry-After"] == str(store.ttl)


@pytest.mark.usefixtures("store")
async def test_429_detail_never_leaks_limiter_internals() -> None:
    """The body must not name Redis, the bucket, the key, or the password.

    Same pinning as test_readyz_unit: the Redis URL embeds a password, and
    the web proxies pass api error details to the browser verbatim.
    """
    last: HTTPException | None = None
    for _ in range(11):
        try:
            await ratelimit.enforce("login_ip", "1.2.3.4")
        except HTTPException as exc:
            last = exc
    assert last is not None
    blob = str(last.detail) + str(last.headers)
    for secret in ("redis", "sermon_local_dev", "ratelimit:", "login_ip", "1.2.3.4"):
        assert secret not in blob.lower()


async def test_enforce_keys_are_scoped_per_bucket_and_identity(store: _FakeStore) -> None:
    await ratelimit.enforce("login_ip", "1.2.3.4")
    await ratelimit.enforce("login_ip", "5.6.7.8")
    await ratelimit.enforce("signup_ip", "1.2.3.4")
    assert set(store.counts) == {
        "ratelimit:login_ip:1.2.3.4",
        "ratelimit:login_ip:5.6.7.8",
        "ratelimit:signup_ip:1.2.3.4",
    }


async def test_kill_switch_skips_the_store(
    store: _FakeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ratelimit_enabled", False)
    for _ in range(50):
        await ratelimit.enforce("login_ip", "1.2.3.4")
    assert store.counts == {}


async def test_redis_down_fails_open_with_a_logged_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Availability over throttling: a limiter-store outage must not 500 logins."""

    async def broken(_key: str, _window: int) -> tuple[int, int]:
        msg = "redis://:sermon_local_dev@localhost:63792/2 refused"
        raise ConnectionError(msg)

    monkeypatch.setattr(ratelimit, "_hit", broken)
    with caplog.at_level("WARNING", logger="ratelimit"):
        await ratelimit.enforce("login_ip", "1.2.3.4")  # must not raise
    assert any("failing OPEN" in record.message for record in caplog.records)


@pytest.mark.usefixtures("store")
async def test_rate_string_is_read_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env-driven limits (and test monkeypatches) must not be frozen at import."""
    monkeypatch.setattr(settings, "ratelimit_login_ip", "1/60")
    await ratelimit.enforce("login_ip", "1.2.3.4")
    with pytest.raises(HTTPException):
        await ratelimit.enforce("login_ip", "1.2.3.4")


# ---------------------------------------------------------------------------
# client_ip() — XFF trust gating


def test_client_ip_defaults_to_tcp_peer_and_ignores_xff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: with trust off, a client-supplied XFF can't dodge buckets."""
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    req = _request({"X-Forwarded-For": "203.0.113.9"})
    assert ratelimit.client_ip(req) == "10.0.0.7"


def test_client_ip_uses_rightmost_xff_entry_when_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rightmost = written by our own proxy; leftmost is client-forgeable.

    A client prepending ``spoofed,`` to the list (the append-style proxy
    world) must NOT rotate the bucket key — the proxy-written rightmost
    entry wins. With modern Caddy (replaces inbound XFF) the list is a
    single attested entry and rightmost == leftmost.
    """
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    # Single attested entry (modern Caddy replace behavior).
    req = _request({"X-Forwarded-For": "203.0.113.9"})
    assert ratelimit.client_ip(req) == "203.0.113.9"
    # Append-style world: client prepended a spoof; rightmost still wins.
    req = _request({"X-Forwarded-For": "6.6.6.6, 203.0.113.9"})
    assert ratelimit.client_ip(req) == "203.0.113.9"
    # Spoof rotation attempt: different leftmost, same attested rightmost
    # -> same bucket key.
    req2 = _request({"X-Forwarded-For": "7.7.7.7, 203.0.113.9"})
    assert ratelimit.client_ip(req2) == ratelimit.client_ip(req)


def test_client_ip_trusted_but_no_header_falls_back_to_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    assert ratelimit.client_ip(_request()) == "10.0.0.7"
    assert ratelimit.client_ip(_request({"X-Forwarded-For": "  "})) == "10.0.0.7"


def test_client_ip_without_peer_is_a_stable_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    assert ratelimit.client_ip(_request(client_host=None)) == "unknown"


# ---------------------------------------------------------------------------
# bucket registry contract


def test_every_bucket_has_a_parseable_settings_field() -> None:
    """Registry ↔ settings consistency — a bucket without a field is a wiring bug."""

    for bucket, getter in ratelimit._BUCKETS.items():
        limit, window = parse_rate(getter())
        assert limit >= 1 and window >= 1, bucket


def test_ip_limit_rejects_unknown_buckets_at_wiring_time() -> None:
    with pytest.raises(KeyError, match="no-such-bucket"):
        ratelimit.ip_limit("no-such-bucket")


# ---------------------------------------------------------------------------
# route wiring — the dependencies sit on the real app and fire pre-handler


def _exhaust(store: _FakeStore, key: str, limit: int) -> None:
    store.counts[key] = limit  # next hit is limit+1 → over


class _NoUserResult:
    """Quacks like a SQLAlchemy result whose query matched no row."""

    def scalar_one_or_none(self) -> None:
        return None


class _NoUserSession:
    async def execute(self, _stmt: object) -> _NoUserResult:
        return _NoUserResult()


async def _no_user_session() -> AsyncIterator[_NoUserSession]:
    """``auth._session`` override: a DB-free session that finds no user.

    Lets an UNDER-limit login request run the real handler to its uniform
    401 without Postgres — proving the request passed the limiter and died
    on credentials, not on a 429.
    """
    yield _NoUserSession()


def test_login_route_is_limited_per_ip(store: _FakeStore) -> None:
    client = TestClient(main_module.app)  # bare client: no lifespan, no guards
    body = {"email": "a@example.com", "password": "irrelevant-1"}
    _exhaust(store, "ratelimit:login_ip:testclient", 10)
    response = client.post("/auth/login", json=body)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(store.ttl)
    assert response.json() == {"detail": "Too many requests. Please try again shortly."}


def test_signup_route_is_limited_per_ip(store: _FakeStore) -> None:
    client = TestClient(main_module.app)
    _exhaust(store, "ratelimit:signup_ip:testclient", 5)
    response = client.post(
        "/auth/signup",
        json={"email": "a@example.com", "password": "longenough-1"},
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(store.ttl)


def test_login_keys_on_forwarded_ip_only_when_trusted(
    store: _FakeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(main_module.app)
    body = {"email": "a@example.com", "password": "irrelevant-1"}

    # Trust ON (prod-behind-proxy posture): the forwarded address is the key.
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    _exhaust(store, "ratelimit:login_ip:203.0.113.9", 10)
    response = client.post("/auth/login", json=body, headers={"X-Forwarded-For": "203.0.113.9"})
    assert response.status_code == 429

    # Trust OFF (default): the same header is ignored — the spoofed bucket
    # being full must NOT 429 the TCP peer's own (empty) bucket. The request
    # then runs the real handler against a DB-free no-user session → 401.
    monkeypatch.setitem(main_module.app.dependency_overrides, auth._session, _no_user_session)
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    response = client.post("/auth/login", json=body, headers={"X-Forwarded-For": "203.0.113.9"})
    assert response.status_code == 401
    assert store.counts["ratelimit:login_ip:testclient"] == 1


def test_search_summary_is_limited_per_user_not_ip(
    store: _FakeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 fires keyed on the JWT user_id, before retrieval/LLM run.

    ``get_current_user`` is overridden (FastAPI's sanctioned seam — the
    Depends() reference is captured at import, so monkeypatching the module
    attribute can't intercept it); the handler body would need Postgres +
    Milvus + an LLM key, so a passing 429 here also proves the limiter runs
    BEFORE the expensive pipeline.
    """
    user = User(email="limited@example.test", password_hash="x")
    user.user_id = uuid.uuid4()

    async def fake_user() -> User:
        return user

    monkeypatch.setitem(main_module.app.dependency_overrides, auth.get_current_user, fake_user)
    client = TestClient(main_module.app)
    _exhaust(store, f"ratelimit:summary_user:{user.user_id}", 5)
    response = client.post("/search-summary", json={"query": "what is faith"})
    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(store.ttl)
    assert response.json() == {"detail": "Too many requests. Please try again shortly."}
    # Keyed on user_id — the client's IP bucket is untouched.
    assert not any("testclient" in key for key in store.counts)


def test_healthz_and_readyz_are_not_rate_limited(store: _FakeStore) -> None:
    """Probe routes stay unlimited — compose HEALTHCHECK / k8s poll them."""
    client = TestClient(main_module.app)
    for _ in range(3):
        assert client.get("/healthz").status_code == 200
    assert store.counts == {}
