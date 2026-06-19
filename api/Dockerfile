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
#
# Phase 43/29: the editor's .docx export/import endpoints shell out to
# worker/convert.py, which runs pandoc <-> a React-free Node leg
# (worker/convert_node/cli.mjs, @tiptap/html on Node >=22). So this image
# now also bakes pandoc, a Node 22 runtime, AND a clean node_modules for the
# convert_node bundle. The host node_modules is stripped by .dockerignore
# (the `**/node_modules` glob is intentional), so the `node_deps` stage below
# rebuilds it hermetically with `npm ci --omit=dev` against the COMMITTED
# convert_node/package.json + package-lock.json. Still NO ML weights.

# --- node_modules builder: hermetic npm ci for the convert_node bundle ----
# Pinned Node 22 (matches convert_node/package.json "engines": node >=22).
FROM node:22.21.0-slim AS node_deps
WORKDIR /app/worker/convert_node
# Only the manifest + lockfile are needed for a reproducible, cache-friendly
# `npm ci`; the .mjs source arrives in the final stage via `COPY worker/`.
COPY worker/convert_node/package.json worker/convert_node/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

# --- python deps -----------------------------------------------------------
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

# --- final runtime ---------------------------------------------------------
FROM python:3.12-slim
# ca-certificates: HTTPS to the inference endpoints (api.deepinfra.com) and
# the LLM endpoint (generativelanguage.googleapis.com / api.ppq.ai); also the
# NodeSource apt repo below.
# libmagic1: the Phase 20 /upload content sniff — python-magic is a ctypes
# wrapper, the shared library is a system package (worker/AGENTS.md
# "System binaries"; GitHub's ubuntu runners ship it with `file`).
# pandoc:    Phase 43 .docx export/import shells out via pypandoc
#            (worker/convert.py) — the HTML <-> docx leg of the round-trip.
# curl + gnupg: pull and trust the NodeSource signing key so we can install a
#            current Node 22 (the convert_node @tiptap/html leg needs >=22;
#            Debian bookworm's nodejs is far older).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates libmagic1 pandoc curl gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    # curl + gnupg were only needed to add the repo; drop them to keep the
    # runtime surface small. pandoc/libmagic1/nodejs/ca-certificates stay.
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deps /app/api/.venv /app/api/.venv
# worker/ first (brings convert.py, convert_node/*.mjs, assets/reference.docx),
# then the hermetic node_modules on top — the host copy was stripped by
# .dockerignore, so this clean tree is the one convert.py resolves.
COPY worker/ /app/worker/
COPY --from=node_deps /app/worker/convert_node/node_modules /app/worker/convert_node/node_modules
COPY api/ /app/api/
ENV PATH="/app/api/.venv/bin:$PATH" \
    PYTHONPATH=/app/worker \
    PYTHONUNBUFFERED=1
# Drop privileges: the app needs no write access to its own tree at runtime.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser
WORKDIR /app/api
EXPOSE 8000
# Readiness, not liveness: /readyz flips on Postgres/Milvus/provider trouble
# (api/readyz.py), whereas /healthz only proves the process is up. Probe with
# the venv's Python (no curl in the final image) — exit 0 only on HTTP 200.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=4).status == 200 else 1)"]
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
