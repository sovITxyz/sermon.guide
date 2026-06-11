"""GET /readyz — readiness probe over the three runtime dependencies.

``/healthz`` answers "is the process alive" and stays dependency-free;
this route answers "can the process serve real traffic" by probing
Postgres, Milvus, and Redis concurrently, each under a short timeout.
Phase 29 points the container HEALTHCHECK here; Phase 30 wires the k8s
readinessProbe to it.

Shape contract (orchestrators consume this — keep it stable):

- 200 ``{"status": "ready", "deps": {"postgres": "ok", "milvus": "ok",
  "redis": "ok"}}`` when every dependency answers within budget.
- 503 with the same envelope and ``"down"`` per failing dependency.

Probe details:

- Postgres: ``SELECT 1`` through the shared async engine
  (``db.get_session_factory`` — the same pool request handlers use),
  bounded by ``asyncio.wait_for`` (asyncpg is natively cancellable).
- Milvus: ``has_collection`` on ``library_vectors``. pymilvus is
  blocking, so the call runs in ``asyncio.to_thread`` and the bound is
  pymilvus's own per-call ``timeout`` kwarg — a ``wait_for`` alone
  can't cancel a thread, the RPC deadline must do the timing out. The
  boolean result is deliberately ignored: this is a connectivity probe,
  not a bootstrap check (``make bootstrap-milvus`` owns the schema).
- Redis: ``PING`` via ``redis.asyncio`` against the broker URL from
  ``tasks_client.RedisSettings`` (the same settings Celery enqueues
  with), with hard socket connect/read timeouts.

Failure detail never reaches the response body — connection errors can
embed hosts and DSNs (the Redis URL carries a password), so the body
says only ``"down"`` and the exception goes to the server log.
"""

# pymilvus 2.6 ships without `py.typed`; same relaxation as search.py.
# redis.asyncio command methods return the `ResponseT = Awaitable | Any`
# union, which pyright strict reports as partially Unknown.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import redis.asyncio as aioredis
from db import get_session_factory
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pymilvus import MilvusClient
from scripts.bootstrap_milvus import COLLECTION_NAME, make_client
from sqlalchemy import text

from tasks_client import RedisSettings

router = APIRouter(tags=["meta"])

logger = logging.getLogger(__name__)

# Per-dependency budget, seconds. Short so a wedged dependency flips the
# probe to 503 quickly instead of stalling the orchestrator's poll loop
# (the prod compose healthcheck allows 5s total; three concurrent 2s
# probes fit with headroom).
PROBE_TIMEOUT_SECONDS = 2.0

# Process-wide Milvus client for probing — lazy for the same reason as
# search.py: import time must not require a reachable Milvus
# (construction is offline; the first RPC connects).
_milvus_client: MilvusClient | None = None


def _milvus() -> MilvusClient:
    global _milvus_client  # noqa: PLW0603 — module-level singleton, see module docstring
    if _milvus_client is None:
        _milvus_client = make_client()
    return _milvus_client


async def _probe_postgres() -> None:
    async def ping() -> None:
        session_factory = get_session_factory()
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))

    await asyncio.wait_for(ping(), timeout=PROBE_TIMEOUT_SECONDS)


async def _probe_milvus() -> None:
    def ping() -> None:
        # `_ =` because pymilvus mis-annotates this sync method as returning
        # a coroutine (see scripts/bootstrap_milvus.py's header note).
        _ = _milvus().has_collection(
            collection_name=COLLECTION_NAME,
            timeout=PROBE_TIMEOUT_SECONDS,
        )

    await asyncio.to_thread(ping)


async def _probe_redis() -> None:
    client = aioredis.Redis.from_url(
        RedisSettings().url(0),
        socket_connect_timeout=PROBE_TIMEOUT_SECONDS,
        socket_timeout=PROBE_TIMEOUT_SECONDS,
    )
    try:
        await client.ping()
    finally:
        await client.aclose()


async def _run_probe(name: str, probe: Callable[[], Awaitable[None]]) -> str:
    """Map any probe failure — timeout, refused, DNS — to a flat ``"down"``."""
    try:
        await probe()
    except Exception:  # noqa: BLE001 — a readiness probe must never raise; reason logged, not leaked
        logger.warning("readiness probe failed: %s", name, exc_info=True)
        return "down"
    return "ok"


@router.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness probe. 200 only when Postgres + Milvus + Redis all answer."""
    # Built per-request from module globals so tests can monkeypatch the
    # individual ``_probe_*`` seams (a module-level dict would freeze the
    # original function references at import time).
    probes: dict[str, Callable[[], Awaitable[None]]] = {
        "postgres": _probe_postgres,
        "milvus": _probe_milvus,
        "redis": _probe_redis,
    }
    results = await asyncio.gather(
        *(_run_probe(name, probe) for name, probe in probes.items()),
    )
    deps = dict(zip(probes.keys(), results, strict=True))
    all_ok = all(state == "ok" for state in deps.values())
    return JSONResponse(
        status_code=status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ready" if all_ok else "unready", "deps": deps},
    )
