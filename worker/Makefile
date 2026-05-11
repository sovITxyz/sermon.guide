# worker/ — uv-driven lint, type, test, and Milvus bootstrap shortcuts.
#
# `uv run` picks up the project venv automatically; no need to activate it.
# `bootstrap-milvus` sources infra/.env so the script's SERMON_MILVUS_*
# defaults can be overridden without exporting them in your shell. Pass
# extra args via `make bootstrap-milvus ARGS=--force`.

.PHONY: lint format format-check typecheck test test-isolation bootstrap-milvus

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
