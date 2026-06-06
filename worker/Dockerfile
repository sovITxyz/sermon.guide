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
# BGE-Large (~1.3GB) is NOT baked — it lives in the shared `sermon-hf-cache`
# volume (see api/Dockerfile rationale). NLTK WordNet (~40MB) IS baked, so
# the first dedup signature() never does a runtime download.

FROM python:3.12-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
WORKDIR /app/worker
COPY worker/pyproject.toml worker/uv.lock ./
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
# pandoc:    EPUB extraction shells out via pypandoc (extractors/epub.py).
# libmagic1: MIME sniffing via python-magic (extractors/extract.py) — NOT
#            preinstalled on -slim images.
# libgomp1:  OpenMP runtime for torch-CPU / sentence-transformers wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends pandoc libmagic1 libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deps /app/worker/.venv /app/worker/.venv
COPY worker/ /app/worker/
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
# --concurrency=1: each prefork child that ingests a new book loads BGE-Large
# up to TWICE (chunking boundary-embedder + chunk embedder) ≈ 3-4GB resident;
# the shared 16GB box can't afford parallel ingests alongside the api's models.
# --time-limit: hard kill any single ingest at 1h (soft at 55min) so one
# poisoned/oversized upload can't pin the CPU forever (celery_app.py already
# sets acks_late + reject_on_worker_lost, so a killed task is not silently lost).
CMD ["celery", "-A", "celery_app", "worker", "--loglevel=info", \
     "--concurrency=1", "--time-limit=3600", "--soft-time-limit=3300"]
