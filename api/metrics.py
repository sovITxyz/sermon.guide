"""Prometheus exposition — ``GET /metrics`` + the collectors the api feeds (Phase 27).

This module owns ALL Prometheus metrics for the platform. The worker emits
no Prometheus client lib (no pushgateway, and a short-lived prefork child's
in-memory registry is not scrapable — see ``worker/obs.py`` rationale); it
contributes ingest observability purely through correlated JSON logs. Queue
depth is still visible here because the api reads the broker Redis ``LLEN``
on scrape, needing no worker cooperation.

Collectors (all on the default ``REGISTRY``, declared once at module scope so
re-import never raises ``Duplicated timeseries``):

- ``REQUEST_DURATION`` — per-route HTTP latency histogram, labelled by the
  matched APIRoute path TEMPLATE (``/tasks/{task_id}``, never the raw path —
  otherwise every UUID becomes a label value and Prometheus memory explodes).
  Fed by the correlation/latency middleware in ``observability.py``. Buckets
  are tuned to the ARCHITECTURE.md §1 targets (the <50ms vector / <1s summary
  targets AND the multi-second reality are all visible).
- ``RETRIEVAL_STAGE`` — per-stage retrieval latency histogram (embed / dense /
  sparse / rerank / highlight / llm), fed by ``search.py`` + ``summary.py``.
- ``RETRIEVAL_DEGRADED`` — Phase 22 degraded-arm counter, incremented at each
  ``run_search`` degraded site. A non-zero counter under healthy deps is the
  in-our-code-bug tell (the Phase 22 trust-gap mitigation).
- ``CELERY_QUEUE_DEPTH`` — Celery backlog gauge, refreshed on scrape via Redis
  ``LLEN`` on the broker. Backlog APPROXIMATION, not exact depth: ``acks_late``
  + ``visibility_timeout`` mean an in-flight task's message is off the list,
  and non-default queues are not counted.

The ``/metrics`` route is genuinely public + unlimited — same posture as
``/healthz`` // ``/readyz`` (compose HEALTHCHECK / k8s probes poll those; a
Prometheus scraper polls this). It is added to the rate-limiter's
deliberately-unlimited set by simply carrying no limiter dependency.
"""

# redis.asyncio command methods return the `ResponseT = Awaitable | Any`
# union, which pyright strict reports as partially Unknown (same accommodation
# as readyz.py / ratelimit.py).
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false

from __future__ import annotations

import logging
from collections.abc import Awaitable
from typing import cast

import redis.asyncio as aioredis
from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

router = APIRouter(tags=["meta"])

logger = logging.getLogger(__name__)

# The default Celery list key on the broker db. Celery's default queue is
# named "celery" and its messages land on a Redis list of the same name when
# the broker is Redis. The api reads the BROKER db (0), never the result
# backend (1) or the limiter db (2).
_CELERY_QUEUE_KEY = "celery"
_DEFAULT_QUEUE_LABEL = "celery"

# Hard socket budget per scrape's queue-depth read. Small: a wedged Redis must
# not stall the scrape — the read fails soft (gauge omitted/left as-is) rather
# than 500 the endpoint.
_SCRAPE_REDIS_TIMEOUT_SECONDS = 1.0

# HTTP latency buckets tuned to the ARCHITECTURE.md §1 targets: the <50ms
# vector and <1s summary targets are resolvable, and the multi-second reality
# (sequential remote-inference legs per summary) stays visible rather than
# saturating a too-low top bucket.
_HTTP_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
)

# Per-route HTTP latency. ``route`` is the matched APIRoute path TEMPLATE, set
# by the middleware (see the cardinality note in the module docstring).
REQUEST_DURATION = Histogram(
    "sermon_api_request_duration_seconds",
    "HTTP request latency in seconds, by matched route template, method, and status.",
    labelnames=("route", "method", "status"),
    buckets=_HTTP_BUCKETS,
)

# Per-stage retrieval latency. ``stage`` ∈ {embed, dense, sparse, rerank,
# highlight, llm}. Default histogram buckets are fine here — these legs span
# sub-second (healthy provider calls) to multi-second (degraded / thinking),
# and per-stage timing is for relative attribution, not the §1 SLO gate.
RETRIEVAL_STAGE = Histogram(
    "sermon_retrieval_stage_duration_seconds",
    "Retrieval pipeline stage latency in seconds, by stage.",
    labelnames=("stage",),
)

# Phase 22 degraded-arm counter. Incremented wherever ``run_search`` appends to
# the degraded list (dense / sparse / rerank / highlight). A non-zero value
# under healthy dependencies means an in-our-code bug is riding as permanent
# degradation (the Phase 22 trust-gap tell — api/AGENTS.md "Open trust gaps").
RETRIEVAL_DEGRADED = Counter(
    "sermon_retrieval_degraded_total",
    "Count of retrieval stages that degraded (Phase 22), by stage.",
    labelnames=("stage",),
)

# Celery backlog gauge — refreshed on scrape (see ``_refresh_queue_depth``).
# APPROXIMATION: LLEN undercounts in-flight (acks_late) messages and ignores
# non-default queues; the help string says so.
CELERY_QUEUE_DEPTH = Gauge(
    "sermon_celery_queue_depth",
    "Approximate Celery broker queue depth (Redis LLEN; undercounts in-flight "
    "acks_late messages and ignores non-default queues), by queue.",
    labelnames=("queue",),
)


async def _refresh_queue_depth() -> None:
    """Read the broker's default-queue ``LLEN`` into the gauge. Fail soft.

    Same ``redis.asyncio`` client pattern ``readyz.py`` uses, against the
    BROKER db (0). A Redis error never 500s the scrape — it logs and leaves
    the gauge at its prior value (the readiness-probe never-raise posture).

    ``RedisSettings`` is imported LAZILY here (not at module scope) to break an
    import cycle: ``tasks_client`` imports ``observability`` (for the
    correlation-key mirror), ``observability`` imports this module (for
    ``REQUEST_DURATION``), and a module-scope ``from tasks_client import
    RedisSettings`` would close the loop. Deferring it to call time keeps the
    cycle open.
    """
    from tasks_client import RedisSettings

    client = aioredis.Redis.from_url(
        RedisSettings().url(0),
        socket_connect_timeout=_SCRAPE_REDIS_TIMEOUT_SECONDS,
        socket_timeout=_SCRAPE_REDIS_TIMEOUT_SECONDS,
    )
    try:
        # ``redis.asyncio`` types command results as the ``ResponseT`` union;
        # cast to ``Awaitable`` so the ``await`` is well-typed under strict
        # (the same accommodation readyz.py makes for ``client.ping()``).
        depth = await cast("Awaitable[int]", client.llen(_CELERY_QUEUE_KEY))
        CELERY_QUEUE_DEPTH.labels(queue=_DEFAULT_QUEUE_LABEL).set(float(int(depth)))
    except Exception:  # noqa: BLE001 — a scrape must never raise; reason logged, gauge left as-is
        logger.warning("celery queue-depth scrape failed; leaving gauge unchanged", exc_info=True)
    finally:
        await client.aclose()


@router.get("/metrics")
async def metrics() -> Response:
    """Prometheus exposition for the platform's HTTP / retrieval / queue metrics.

    Public + unlimited (no auth, no rate-limit dependency) — a scraper polls
    it like an orchestrator polls ``/readyz``. Refreshes the queue-depth gauge
    on each scrape (fail-soft), then renders the default registry.
    """
    await _refresh_queue_depth()
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
