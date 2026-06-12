# worker/ — uv-driven lint, type, test, and Milvus bootstrap shortcuts.
#
# `uv run` picks up the project venv automatically; no need to activate it.
# `bootstrap-milvus` sources infra/.env so the script's SERMON_MILVUS_*
# defaults can be overridden without exporting them in your shell. Pass
# extra args via `make bootstrap-milvus ARGS=--force`.
#
# Test targets, by posture:
# - `test`       keyless-fast; never sources infra/.env, live-gated suites
#                skip silently here BY DESIGN.
# - `test-live`  the keyed live gate (Phase 23): sources infra/.env and
#                FAILS on any key/infra skip — see the target comment.

.PHONY: lint format format-check typecheck test test-live test-isolation test-retrieval-golden bootstrap-milvus backfill-chunks clean-parent-sections sweep-orphans seed-corpus ingest worker enqueue migrate-up migrate-down migrate-new

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

# Phase 23 live gate — closes the Phase 20 deviation (ii) silent-skip trap.
# Sources ../infra/.env (live compose ports + the operator DEEPINFRA_API_KEY;
# the code defaults in db/settings.py and celery_app.py are stale dev-box
# remaps — Phase 21 finding iii) and runs the live-gated suites: golden
# retrieval, ingest e2e (incl. kill-9 redelivery), embedding weight-parity.
# FAILS on ANY key/infra skip; the only tolerated skip is the corpus gap
# ("corpus sample(s) missing") — the same classification as CI's
# retrieval-golden-live guard, implemented in scripts/test_live.sh.
# Prereqs: `make up` at the repo root, then `make migrate-up` +
# `make bootstrap-milvus` here. Expect tens of minutes of wall-clock
# (cold ingest is minutes/book; the warm golden path measured ~26 min —
# Phase 21 finding iv). Plain `test` stays keyless-fast by design.
test-live:
	@test -f ../infra/.env || { \
		echo 'test-live: ../infra/.env not found.' >&2; \
		echo 'test-live: run `make up` at the repo root to create it, set the operator DEEPINFRA_API_KEY in it, then re-run.' >&2; \
		exit 2; }
	set -a && . ../infra/.env && set +a && bash scripts/test_live.sh

# Phase 3 hard gate: multi-tenant isolation smoke test. Requires a live Milvus
# (`make up` from repo root). See ARCHITECTURE.md §7.1 and CLAUDE.md.
test-isolation:
	uv run pytest tests/test_tenant_isolation.py -v

# Phase 11 golden retrieval regression suite. Requires live Milvus + Postgres
# + DEEPINFRA_API_KEY (embeddings are remote since Phase 16b — ADR 0006) + the
# local sample corpus under tests/samples/. Skips cleanly when any of those
# are missing — see the module docstring.
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

# Phase 21 maintenance pair. Both are DESTRUCTIVE (Milvus delete+reinsert /
# orphan deletes), so unlike backfill-chunks they default to a dry-run —
# pass ARGS=--execute to apply.
# - clean-parent-sections: strip HTML debris from chunks.parent_section and
#   the matching Milvus metadata (reuses chunking.clean_heading).
# - sweep-orphans: delete Milvus vectors with no global_books row and empty
#   global_books rows; aborts if any candidate is tenant-reachable.
clean-parent-sections:
	set -a && . ../infra/.env && set +a && uv run python -m scripts.clean_parent_sections $(ARGS)

sweep-orphans:
	set -a && . ../infra/.env && set +a && uv run python -m scripts.sweep_orphans $(ARGS)

# Phase 23 seed: bulk-ingest the public-domain corpus manifest
# (seeds/manifest.jsonl) through the Celery + dedup path with
# deterministic Phase 20 idempotency claims — re-runs converge, crashed
# runs resume under the same book_id. Expects the downloaded EPUBs under
# tests/samples/ (gitignored; the repo holds the manifest only) and a
# running `make worker` in another terminal. Preview: ARGS=--dry-run.
# Full operator recipe: docs/SEED_CORPUS.md.
seed-corpus:
	set -a && . ../infra/.env && set +a && uv run python -m scripts.seed_corpus $(ARGS)

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
