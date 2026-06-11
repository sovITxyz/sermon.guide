"""Unit tests for the ``main.py`` lifespan boot guards.

Phase 18: JWT-secret guard. Phase 19: CORS prod-origin guard — refuses
to pair ``allow_credentials=True`` with a wildcard/unset/loopback origin
outside dev.

The guards run at lifespan time, never at import time — so these tests
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
_REAL_ORIGIN = "https://app.example.com"


def _set_posture(monkeypatch: pytest.MonkeyPatch, env: str, secret: str) -> None:
    monkeypatch.setattr(main_module.settings, "env", env)
    monkeypatch.setattr(main_module.settings, "jwt_secret", secret)


def _set_prod_with_origins(monkeypatch: pytest.MonkeyPatch, origins: list[str]) -> None:
    """Prod posture with a VALID jwt secret so only the CORS guard can trip."""
    _set_posture(monkeypatch, "prod", _REAL_SECRET)
    monkeypatch.setattr(main_module.settings, "cors_origins", origins)


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
    # Real CORS origins too — Phase 19's guard shares the lifespan, and this
    # test pins the JWT guard's allow path, not a CORS refusal.
    _set_prod_with_origins(monkeypatch, [_REAL_ORIGIN])
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


# ---------------------------------------------------------------------------
# Phase 19 — CORS prod-origin guard (credentials + promiscuous origin = no boot)


@pytest.mark.parametrize(
    "origins",
    [
        ["*"],  # Starlette mirrors Origin back for "*" + credentials
        ["https://app.example.com", "*"],  # one wildcard poisons the list
        ["https://*.example.com"],  # allow_origins does exact match; a glob is a misconfig
        [],  # unset/empty — operator never configured the prod origin
        [""],  # empty-string entry (e.g. mis-templated env)
        ["http://localhost:3000"],  # leftover dev default
        ["https://127.0.0.1:3000"],  # loopback in prod = same forgotten-config smell
    ],
)
async def test_cors_guard_refuses_promiscuous_origins_in_prod(
    monkeypatch: pytest.MonkeyPatch,
    origins: list[str],
) -> None:
    _set_prod_with_origins(monkeypatch, origins)
    with pytest.raises(RuntimeError, match="SERMON_API_CORS_ORIGINS"):
        async with main_module.lifespan(main_module.app):
            pass


async def test_cors_guard_allows_exact_prod_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_prod_with_origins(monkeypatch, [_REAL_ORIGIN, "https://www.example.com"])
    async with main_module.lifespan(main_module.app):
        pass


async def test_cors_guard_error_names_the_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal must tell the operator exactly what to set."""
    _set_prod_with_origins(monkeypatch, ["*"])
    with pytest.raises(RuntimeError) as excinfo:
        async with main_module.lifespan(main_module.app):
            pass
    message = str(excinfo.value)
    assert "SERMON_API_CORS_ORIGINS" in message
    assert "SERMON_API_ENV" in message


async def test_cors_guard_dev_boot_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`make dev` shape: dev posture keeps the localhost default working."""
    _set_posture(monkeypatch, "dev", DEV_JWT_SECRET)
    monkeypatch.setattr(main_module.settings, "cors_origins", ["http://localhost:3000"])
    async with main_module.lifespan(main_module.app):
        pass


def test_testclient_startup_runs_the_cors_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CORS guard is wired into app startup, not just an orphan function."""
    _set_prod_with_origins(monkeypatch, ["*"])
    with pytest.raises(RuntimeError, match="SERMON_API_CORS_ORIGINS"), TestClient(main_module.app):
        pass
