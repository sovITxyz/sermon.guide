"""Unit tests for ``/readyz`` (Phase 18) — probe seams mocked, no live infra.

The handler builds its probe table per-request from module globals, so
monkeypatching ``readyz._probe_postgres`` / ``_probe_milvus`` /
``_probe_redis`` swaps a dependency's fate without any network. What's
pinned here is the orchestrator-facing contract (Phases 29/30 consume
it): 200 ``{"status": "ready", "deps": {...}}`` only when all three
probes succeed, 503 with a per-dep ``"down"`` breakdown otherwise, and
no failure detail in the body (connection errors can embed DSNs and the
Redis password).
"""

# Tests exercise module-internals on purpose.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from typing import Any

import pytest

import readyz as readyz_module

_DEPS = ("postgres", "milvus", "redis")


async def _ok() -> None:
    return None


def _down(exc: Exception) -> Any:
    async def probe() -> None:
        raise exc

    return probe


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    postgres: Any = _ok,
    milvus: Any = _ok,
    redis: Any = _ok,
) -> None:
    monkeypatch.setattr(readyz_module, "_probe_postgres", postgres)
    monkeypatch.setattr(readyz_module, "_probe_milvus", milvus)
    monkeypatch.setattr(readyz_module, "_probe_redis", redis)


async def _call() -> tuple[int, dict[str, Any]]:
    response = await readyz_module.readyz()
    return response.status_code, json.loads(bytes(response.body))


async def test_all_deps_up_returns_200_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch)
    status_code, body = await _call()
    assert status_code == 200
    assert body == {"status": "ready", "deps": dict.fromkeys(_DEPS, "ok")}


@pytest.mark.parametrize("failing", _DEPS)
async def test_single_dep_down_returns_503_naming_it(
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
) -> None:
    _patch(monkeypatch, **{failing: _down(ConnectionError("refused"))})
    status_code, body = await _call()
    assert status_code == 503
    assert body["status"] == "unready"
    expected = {name: ("down" if name == failing else "ok") for name in _DEPS}
    assert body["deps"] == expected


async def test_all_deps_down_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    boom = _down(ConnectionError("refused"))
    _patch(monkeypatch, postgres=boom, milvus=boom, redis=boom)
    status_code, body = await _call()
    assert status_code == 503
    assert body == {"status": "unready", "deps": dict.fromkeys(_DEPS, "down")}


async def test_timeout_maps_to_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """``asyncio.wait_for`` raises TimeoutError — same flat ``"down"``."""
    _patch(monkeypatch, postgres=_down(TimeoutError()))
    status_code, body = await _call()
    assert status_code == 503
    assert body["deps"]["postgres"] == "down"


async def test_failure_detail_never_reaches_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe error can carry a DSN/password — the body must not."""
    secret_detail = "redis://:sermon_local_dev@localhost:63792/0"  # noqa: S105
    _patch(monkeypatch, redis=_down(ConnectionError(secret_detail)))
    _status_code, body = await _call()
    assert secret_detail not in json.dumps(body)
    assert body["deps"]["redis"] == "down"
