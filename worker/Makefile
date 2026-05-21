# worker/ — uv-driven lint, type, test, and Milvus bootstrap shortcuts.
#
# `uv run` picks up the project venv automatically; no need to activate it.
# `bootstrap-milvus` sources infra/.env so the script's SERMON_MILVUS_*
# defaults can be overridden without exporting them in your shell. Pass
# extra args via `make bootstrap-milvus ARGS=--force`.

.PHONY: lint format format-check typecheck test test-isolation test-retrieval-golden bootstrap-milvus backfill-chunks ingest worker enqueue migrate-up migrate-down migrate-new

lint:
	uv run ruff check .

format:
	uv run ruff format .

format-check:
	uv run ruff format --check .

typecheck:
	uv run pyright

test:
	uv run pytest

# Phase 3 hard gate: multi-tenant isolation smoke test. Requires a live Milvus
# (`make up` from repo root). See ARCHITECTURE.md §7.1 and CLAUDE.md.
test-isolation:
	uv run pytest tests/test_tenant_isolation.py -v

# Phase 11 golden retrieval regression suite. Requires live Milvus + Postgres
# + BGE-Large in HF cache + the local sample corpus under tests/samples/.
# Skips cleanly when any of those are missing — see the module docstring.
test-retrieval-golden:
	set -a && . ../infra/.env && set +a && \
		uv run pytest tests/test_retrieval_golden.py -v

bootstrap-milvus:
	set -a && . ../infra/.env && set +a && uv run python scripts/bootstrap_milvus.py $(ARGS)

# Phase 12 backfill: populate the chunks table from Milvus for books
# that were ingested before Phase 12. Idempotent; safe to re-run. Pass
# `--dry-run` via ARGS to preview, or `--book-id <uuid>` to scope to one.
backfill-chunks:
	set -a && . ../infra/.env && set +a && uv run python -m scripts.backfill_chunks $(ARGS)

# Single-book dedup-aware ingest CLI (Phase 8):
#   detect → extract → MinHash dedup → (skip on hit | chunk → embed → insert) →
#   write global_books + user_library.
# Required: FILE=<path> USER=<user_uuid>. The book_id is decided by dedup, not
# the caller; pass an existing user UUID from `users.user_id`.
ingest:
	set -a && . ../infra/.env && set +a && \
		uv run python -m ingest "$(FILE)" --user-id "$(USER)"

# Phase 9 Celery worker.
#   worker:   long-running prefork worker — Redis broker, Phase 6/8 pipeline.
#             Loglevel info; INT/TERM stops it cleanly (TERM = warm shutdown,
#             waits for in-flight tasks; KILL drops in-flight messages back
#             to the broker after the visibility timeout — see celery_app.py).
#   enqueue:  test producer for `make worker`. Required: FILE=<path> TENANT=<id>.
#             TENANT accepts a users.user_id UUID, or a friendly label like
#             `tenant_a` (deterministically uuid5'd + upserted into users so
#             the FK resolves — see scripts/enqueue_ingest.py).
worker:
	set -a && . ../infra/.env && set +a && \
		uv run celery -A celery_app worker --loglevel=info

enqueue:
	@test -n "$(FILE)" || (echo 'usage: make enqueue FILE=<path> TENANT=<id|label>' && exit 1)
	@test -n "$(TENANT)" || (echo 'usage: make enqueue FILE=<path> TENANT=<id|label>' && exit 1)
	set -a && . ../infra/.env && set +a && \
		uv run python -m scripts.enqueue_ingest "$(FILE)" --tenant "$(TENANT)"

# Alembic migrations. Connection URL is overridden in db/alembic/env.py from
# DBSettings, so these targets source infra/.env to pick up SERMON_POSTGRES_*.
# - migrate-up:   alembic upgrade head (idempotent; no-op when already at head)
# - migrate-down: alembic downgrade -1 (one revision back; use REV=base to wipe)
# - migrate-new:  alembic revision --autogenerate -m "$MSG" (review the diff!)
migrate-up:
	set -a && . ../infra/.env && set +a && uv run alembic upgrade head

migrate-down:
	set -a && . ../infra/.env && set +a && uv run alembic downgrade $(or $(REV),-1)

migrate-new:
	@test -n "$(MSG)" || (echo 'usage: make migrate-new MSG="describe change"' && exit 1)
	set -a && . ../infra/.env && set +a && uv run alembic revision --autogenerate -m "$(MSG)"
