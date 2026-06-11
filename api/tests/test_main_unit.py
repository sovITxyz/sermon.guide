"""Unit tests for the ``main.py`` lifespan boot guard (Phase 18).

The guard runs at lifespan time, never at import time — so these tests
either enter the lifespan contextmanager directly or boot the app
through ``with TestClient(app):`` (context-manager entry executes the
lifespan exactly like uvicorn does; a bare import or bare
``TestClient(app).get(...)`` does not). Settings are monkeypatched as
ATTRIBUTES on the module singleton, the suite-wide convention — the
env-parsing layer is covered in ``test_settings_unit.py``.
"""

# Tests exercise module-internals on purpose.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main as main_module
from settings import DEV_JWT_SECRET

_REAL_SECRET = "a" * 96  # shaped like `openssl rand -hex 48` output


def _set_posture(monkeypatch: pytest.MonkeyPatch, env: str, secret: str) -> None:
    monkeypatch.setattr(main_module.settings, "env", env)
    monkeypatch.setattr(main_module.settings, "jwt_secret", secret)


async def test_guard_refuses_dev_default_secret_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The publicly-known placeholder must never sign prod JWTs."""
    _set_posture(monkeypatch, "prod", DEV_JWT_SECRET)
    with pytest.raises(RuntimeError, match="SERMON_API_JWT_SECRET"):
        async with main_module.lifespan(main_module.app):
            pass


async def test_guard_refuses_empty_secret_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_posture(monkeypatch, "prod", "")
    with pytest.raises(RuntimeError, match="SERMON_API_JWT_SECRET"):
        async with main_module.lifespan(main_module.app):
            pass


async def test_guard_error_names_both_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal must tell the operator exactly what to set."""
    _set_posture(monkeypatch, "prod", DEV_JWT_SECRET)
    with pytest.raises(RuntimeError) as excinfo:
        async with main_module.lifespan(main_module.app):
            pass
    message = str(excinfo.value)
    assert "SERMON_API_JWT_SECRET" in message
    assert "SERMON_API_ENV" in message


async def test_guard_allows_real_secret_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_posture(monkeypatch, "prod", _REAL_SECRET)
    async with main_module.lifespan(main_module.app):
        pass


async def test_guard_allows_dev_opt_out_with_default_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_posture(monkeypatch, "dev", DEV_JWT_SECRET)
    async with main_module.lifespan(main_module.app):
        pass


def test_testclient_startup_runs_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is wired into app startup, not just an orphan function."""
    _set_posture(monkeypatch, "prod", DEV_JWT_SECRET)
    with pytest.raises(RuntimeError, match="SERMON_API_JWT_SECRET"), TestClient(main_module.app):
        pass


def test_dev_opt_out_boots_and_serves_healthz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`make dev` shape: dev posture + placeholder secret boots cleanly."""
    _set_posture(monkeypatch, "dev", DEV_JWT_SECRET)
    with TestClient(main_module.app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


async def test_healthz_stays_dependency_free() -> None:
    """Liveness must answer without settings, DB, Redis, or Milvus."""
    assert await main_module.healthz() == {"status": "ok"}
