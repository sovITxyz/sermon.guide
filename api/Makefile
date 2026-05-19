# api/ — FastAPI HTTP layer for sermon.guide (Phase 10+).
#
# Mirrors worker/Makefile patterns: `uv run` picks up the project venv;
# `make dev` sources infra/.env so SERMON_* defaults can be overridden
# without exporting them in the shell.
#
# Import path note: api/ imports `db` from ../worker (the one
# cross-package import — see root CLAUDE.md). pyproject.toml's pyright
# `extraPaths` and pytest `pythonpath` cover lint/test; uvicorn runtime
# needs PYTHONPATH=../worker too, set explicitly below.

.PHONY: dev lint format format-check typecheck test

PYTHONPATH_WITH_WORKER := ../worker

dev:
	set -a && . ../infra/.env && set +a && \
		PYTHONPATH=$(PYTHONPATH_WITH_WORKER):$$PYTHONPATH \
		uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000

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
