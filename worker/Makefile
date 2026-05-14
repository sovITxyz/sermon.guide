# worker/ — uv-driven lint, type, test, and Milvus bootstrap shortcuts.
#
# `uv run` picks up the project venv automatically; no need to activate it.
# `bootstrap-milvus` sources infra/.env so the script's SERMON_MILVUS_*
# defaults can be overridden without exporting them in your shell. Pass
# extra args via `make bootstrap-milvus ARGS=--force`.

.PHONY: lint format format-check typecheck test test-isolation bootstrap-milvus ingest migrate-up migrate-down migrate-new

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

bootstrap-milvus:
	set -a && . ../infra/.env && set +a && uv run python scripts/bootstrap_milvus.py $(ARGS)

# Single-book dedup-aware ingest CLI (Phase 8):
#   detect → extract → MinHash dedup → (skip on hit | chunk → embed → insert) →
#   write global_books + user_library.
# Required: FILE=<path> USER=<user_uuid>. The book_id is decided by dedup, not
# the caller; pass an existing user UUID from `users.user_id`.
ingest:
	set -a && . ../infra/.env && set +a && \
		uv run python -m ingest "$(FILE)" --user-id "$(USER)"

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
