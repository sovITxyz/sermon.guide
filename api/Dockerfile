# api/ — FastAPI HTTP layer. BUILD FROM THE REPO ROOT:
#
#   docker build -f api/Dockerfile .
#
# The context must be the repo root because api/ imports `db`, `embedding`,
# `retrieval`, and `scripts.bootstrap_milvus` from ../worker at RUNTIME (the
# one allowed cross-package import — root CLAUDE.md). A `COPY api/`-only
# build fails at boot with ModuleNotFoundError.
#
# Deps install from api/uv.lock with `uv sync --frozen` so the
# [tool.uv.sources] CPU-only torch index is honored — re-resolving (or pip)
# would drag in ~2GB of unused CUDA wheels.
#
# The three inference models (BGE-Large ~1.3GB, ms-marco cross-encoder
# ~90MB, BGE-M3 ~2.3GB) are NOT baked into this image. They live in the
# shared `sermon-hf-cache` volume (HF_HOME), downloaded once by the
# `prewarm` one-shot in infra/docker-compose.prod.yml; the runtime then
# loads them lazily with HF_HUB_OFFLINE=1 so no request ever blocks on
# (or flakes over) a HuggingFace network round-trip.

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
# libgomp1: OpenMP runtime needed by the torch-CPU / sentence-transformers
# native wheels on slim images. ca-certificates: HTTPS to the LLM endpoint
# (generativelanguage.googleapis.com / api.ppq.ai).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deps /app/api/.venv /app/api/.venv
COPY worker/ /app/worker/
COPY api/ /app/api/
ENV PATH="/app/api/.venv/bin:$PATH" \
    PYTHONPATH=/app/worker \
    PYTHONUNBUFFERED=1
WORKDIR /app/api
EXPOSE 8000
# Exactly ONE uvicorn worker: each process lazily loads ~3.7GB of models, so
# --workers N would cost N × 3.7GB on the shared box. Handlers offload
# blocking CPU/Milvus work via asyncio.to_thread, so a single worker serves
# concurrent requests fine; the bottleneck is model wall-time, not async I/O.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
