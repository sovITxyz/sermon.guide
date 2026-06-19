"""Unit tests for ``GET /metrics`` (Phase 27) — no live Redis, no live infra.

Pins the Prometheus exposition contract a scraper consumes: 200 with the
Prometheus content-type, every declared metric family name present, and the
queue-depth gauge populated from a MONKEYPATCHED ``LLEN`` seam (the
``readyz._probe_*`` / ``ratelimit._hit`` convention — never a live Redis),
failing SOFT (non-500) when that seam raises.
"""

# Tests exercise module-internals on purpose. ``redis.asyncio`` + the
# prometheus-client gauge internals expose loosely-typed seams; relax the
# Unknown* reports per-file (the test_search_unit.py / readyz convention).
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false

from __future__ import annotations

from typing import Any

import pytest
from prometheus_client import CONTENT_TYPE_LATEST

import metrics as metrics_module

# Every declared metric family the exposition must list (the base name; the
# histogram emits ``_bucket``/``_count``/``_sum`` and the counter a ``_total``,
# but the family name itself appears in the ``# HELP``/``# TYPE`` lines).
_FAMILIES = (
    "sermon_api_request_duration_seconds",
    "sermon_retrieval_stage_duration_seconds",
    "sermon_retrieval_degraded_total",
    "sermon_celery_queue_depth",
)


class _FakeRedis:
    """Duck-typed ``redis.asyncio.Redis`` answering ``llen`` + ``aclose``."""

    def __init__(self, depth: int) -> None:
        self._depth = depth

    async def llen(self, _key: str) -> int:
        return self._depth

    async def aclose(self) -> None:
        return None


class _BoomRedis:
    def __init__(self) -> None:
        self.closed = False

    async def llen(self, _key: str) -> int:
        msg = "redis://:sermon_local_dev@localhost:63792/0 unreachable"
        raise ConnectionError(msg)

    async def aclose(self) -> None:
        self.closed = True


def _patch_redis(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    """Swap the ``from_url`` factory so no socket is ever opened."""
    monkeypatch.setattr(
        metrics_module.aioredis.Redis,
        "from_url",
        classmethod(lambda _cls, *_a, **_k: client),
    )


async def _call() -> Any:
    return await metrics_module.metrics()


async def test_metrics_returns_prometheus_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_redis(monkeypatch, _FakeRedis(depth=0))
    response = await _call()
    assert response.status_code == 200
    assert response.media_type == CONTENT_TYPE_LATEST


async def test_metrics_exposition_lists_every_declared_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_redis(monkeypatch, _FakeRedis(depth=0))
    response = await _call()
    body = bytes(response.body).decode("utf-8")
    for family in _FAMILIES:
        assert family in body, f"missing metric family {family!r} in exposition"


async def test_queue_depth_gauge_reflects_llen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gauge is populated from the monkeypatched LLEN seam on scrape."""
    _patch_redis(monkeypatch, _FakeRedis(depth=7))
    await _call()
    value = metrics_module.CELERY_QUEUE_DEPTH.labels(queue="celery")._value.get()
    assert value == 7.0


async def test_queue_depth_scrape_fails_soft_non_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Redis error during the scrape must NOT 500 the endpoint (fail-soft)."""
    boom = _BoomRedis()
    _patch_redis(monkeypatch, boom)
    response = await _call()
    # Still a valid 200 exposition — the gauge is just left at its prior value.
    assert response.status_code == 200
    assert response.media_type == CONTENT_TYPE_LATEST
    # The client was still closed in the finally despite the llen failure.
    assert boom.closed is True


async def test_queue_depth_failure_never_leaks_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exposition body never carries the Redis DSN even when LLEN raised."""
    _patch_redis(monkeypatch, _BoomRedis())
    response = await _call()
    body = bytes(response.body).decode("utf-8")
    assert "sermon_local_dev" not in body
    assert "63792" not in body
