# worker/ — Celery ingestion worker. BUILD FROM THE REPO ROOT (kept
# symmetric with api/Dockerfile; the worker itself has no upward deps):
#
#   docker build -f worker/Dockerfile .
#
# The same image serves three roles in infra/docker-compose.prod.yml:
#   1. the long-running Celery worker (default CMD below),
#   2. the one-shot Alembic migration  (`command: alembic upgrade head`),
#   3. the one-shot Milvus bootstrap   (`command: python scripts/bootstrap_milvus.py`).
# All three need cwd == /app/worker because the project is intentionally
# non-packaged ([tool.uv] package = false): `from extractors import …`,
# `import dedup`, `celery -A celery_app` all resolve off the working dir.
#
# Phase 16b (ADR 0006): NO model weights load in this image — chunk +
# boundary embeddings are remote DeepInfra calls (worker/inference.py +
# the llama-index OpenAI-compatible embedder), which dropped the ~1.5GB
# torch/sentence-transformers layer and the hf-cache volume. NLTK WordNet
# (~40MB) IS still baked, so the first dedup signature() never does a
# runtime download. No HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE flags: there is
# no local model to guard, so they would be inert (kept off deliberately).
#
# Phase 29: the DOCX round-trip (convert.py) shells out to a React-free Node
# CLI (convert_node/cli.mjs, @tiptap/html) — so this image also bakes Node 22
# and an `npm ci --omit=dev` of the committed convert_node lockfile. The
# host's convert_node/node_modules is stripped by the root .dockerignore
# (**/node_modules), so the image rebuilds a clean, platform-correct copy from
# package.json + package-lock.json (which arrive via COPY worker/). Same
# recipe as api/Dockerfile — both images run convert.py.

# uv is pinned by digest (no :latest) for reproducible dependency resolution.
FROM python:3.12-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
WORKDIR /app/worker
COPY worker/pyproject.toml worker/uv.lock ./
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
# pandoc:    EPUB extraction shells out via pypandoc (extractors/epub.py), and
#            the Phase 43 DOCX round-trip (convert.py) drives it directly.
# libmagic1: MIME sniffing via python-magic (extractors/extract.py) — NOT
#            preinstalled on -slim images.
# ca-certificates: HTTPS to the remote inference endpoint (api.deepinfra.com)
#            AND to NodeSource for the Node 22 install below.
# curl + gnupg: only needed to fetch + verify the NodeSource setup script.
# Node 22: the DOCX round-trip's Node leg (convert_node/cli.mjs) needs
#            @tiptap/html to (de)serialize ProseMirror JSON <-> HTML with the
#            same extension set as the editor (worker/AGENTS.md "System
#            binaries"). curl/gnupg are purged after the install to keep the
#            attack surface and image size down.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        pandoc libmagic1 ca-certificates curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deps /app/worker/.venv /app/worker/.venv
COPY worker/ /app/worker/
# Rebuild convert_node/node_modules inside the image from the committed
# lockfile (host node_modules was stripped by .dockerignore). --omit=dev: the
# bundle has no devDependencies, but the flag keeps it lean if any are added.
# Pandoc's reference.docx (worker/assets/) and the *.mjs CLI sources arrive via
# the COPY worker/ above.
RUN npm ci --omit=dev --prefix /app/worker/convert_node
ENV PATH="/app/worker/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    NLTK_DATA=/usr/share/nltk_data
# Bake WordNet (+ omw-1.4, which newer NLTK wants for some WordNet ops) so
# dedup.signature() never downloads at runtime. The downloader leaves zips
# that nltk.data.find('corpora/wordnet') can NOT resolve (verified against
# this image) — extract and drop them, or the first signature() call tries
# a network download anyway.
RUN python -m nltk.downloader -d /usr/share/nltk_data wordnet omw-1.4 \
    && python -c "import zipfile, pathlib; \
[(zipfile.ZipFile(z).extractall(z.parent), z.unlink()) \
 for z in pathlib.Path('/usr/share/nltk_data/corpora').glob('*.zip')]"
WORKDIR /app/worker
# Drop privileges: the worker only reads its app dir / venv / nltk_data (all
# world-readable from the root-run COPY + installs above) and writes only to
# /tmp (pandoc scratch). alembic-migrate and bootstrap-milvus, the image's two
# alternate one-shot commands, are equally read-only against the filesystem.
RUN useradd --create-home --uid 10001 worker
USER worker
# --concurrency=1: ingest is network-bound now (remote embeddings), not the
# 3-4GB-resident model loads of the in-process era, but one-at-a-time keeps
# pandoc/pymupdf CPU spikes and provider request bursts predictable on the
# small shared box; raise deliberately when throughput data demands it.
# --time-limit: hard kill any single ingest at 1h (soft at 55min) so one
# poisoned/oversized upload can't pin the worker forever (celery_app.py
# already sets acks_late + reject_on_worker_lost, so a killed task is not
# silently lost).
CMD ["celery", "-A", "celery_app", "worker", "--loglevel=info", \
     "--concurrency=1", "--time-limit=3600", "--soft-time-limit=3300"]
