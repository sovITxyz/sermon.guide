# api/ — FastAPI HTTP layer. BUILD FROM THE REPO ROOT:
#
#   docker build -f api/Dockerfile .
#
# The context must be the repo root because api/ imports `db`, `embedding`,
# `inference`, `retrieval`, and `scripts.bootstrap_milvus` from ../worker at
# RUNTIME (the one allowed cross-package import — root CLAUDE.md). A
# `COPY api/`-only build fails at boot with ModuleNotFoundError.
#
# Deps install from api/uv.lock with `uv sync --frozen` for reproducible
# builds.
#
# Phase 16b (ADR 0006): NO model weights live in (or near) this image —
# embeddings/rerank/highlight are remote DeepInfra calls through
# worker/inference.py, and the LLM was already remote (ADR 0005). The
# ~1.5GB torch + sentence-transformers layer, the shared hf-cache volume,
# and the prewarm one-shot are gone; ca-certificates is what keeps the
# HTTPS legs (DeepInfra + LLM endpoints) verifiable.

FROM python:3.12-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
WORKDIR /app/api
COPY api/pyproject.toml api/uv.lock ./
# package=false in pyproject: this installs dependencies only (no project
# build), which is exactly what we want. --no-dev skips pytest/ruff/pyright.
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
# ca-certificates: HTTPS to the inference endpoints (api.deepinfra.com) and
# the LLM endpoint (generativelanguage.googleapis.com / api.ppq.ai).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deps /app/api/.venv /app/api/.venv
COPY worker/ /app/worker/
COPY api/ /app/api/
ENV PATH="/app/api/.venv/bin:$PATH" \
    PYTHONPATH=/app/worker \
    PYTHONUNBUFFERED=1
WORKDIR /app/api
EXPOSE 8000
# ONE uvicorn worker is still plenty: handlers offload blocking remote-call
# and Milvus work via asyncio.to_thread, so a single worker serves concurrent
# requests fine. The Phase 16b RAM rationale (N × 3.7GB of models) is gone,
# but the box is small and the bottleneck is provider wall-time, not async
# I/O — revisit only if traffic data says otherwise.
#
# --no-proxy-headers (Phase 19): uvicorn DEFAULTS proxy_headers to on, which
# silently rewrites request.client from X-Forwarded-For for peers in its
# allow-list (default 127.0.0.1) — an invisible second trust knob that let
# loopback clients spoof the per-IP rate-limit buckets in dev. Disabling it
# makes request.client always the true TCP peer; XFF trust is exclusively
# the explicit, env-gated SERMON_API_TRUST_PROXY_HEADERS decision in
# ratelimit.client_ip (api/AGENTS.md "Rate limiting").
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
