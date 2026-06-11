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

# --no-proxy-headers: uvicorn's default proxy_headers=on rewrites
# request.client from X-Forwarded-For for 127.0.0.1 peers — a hidden trust
# knob that would let loopback clients spoof the Phase 19 per-IP rate-limit
# buckets. XFF trust is exclusively the env-gated
# SERMON_API_TRUST_PROXY_HEADERS decision (ratelimit.client_ip); keep the
# flag in lockstep with api/Dockerfile's CMD.
dev:
	set -a && . ../infra/.env && set +a && \
		PYTHONPATH=$(PYTHONPATH_WITH_WORKER):$$PYTHONPATH \
		uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000 --no-proxy-headers

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
