"""FastAPI application entrypoint — health, CORS, route mounting, error mapping.

Run for local dev with ``make dev`` (uvicorn --reload). Production
deployment is out of scope for Phase 10 — k8s manifests land alongside
KEDA in a later phase.

Phase 16b (ADR 0006): the remote-inference exceptions from
``worker/inference.py`` are mapped here once — embeddings, rerank, and
highlight all raise the same taxonomy, so a single pair of handlers
replaces per-route guards. Unconfigured (no ``DEEPINFRA_API_KEY``) →
503 naming the env var, mirroring ADR 0005's 503-before-retrieval
guard; an upstream failure after retry → 502 naming the provider + leg
(the Phase 14b pattern). The messages come from the exception and never
carry key material or request content.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from inference import MissingInferenceKeyError, RemoteInferenceError

import auth
import calendar_routes
import documents
import library
import reader
import readyz
import search
import summary
import uploads
from settings import DEV_JWT_SECRET, settings


def _guard_jwt_secret() -> None:
    """Refuse to serve forgeable JWTs (Phase 18).

    The dev default secret is public (it lives in this repo): anyone who has
    read the source can mint a valid token for any ``user_id`` — a total
    tenant-isolation defeat. Unset/empty is equally fatal. Only the explicit
    ``SERMON_API_ENV=dev`` opt-out may boot without a real secret.
    """
    if settings.env == "dev":
        return
    if settings.jwt_secret and settings.jwt_secret != DEV_JWT_SECRET:
        return
    msg = (
        "Refusing to start: SERMON_API_JWT_SECRET is unset or still the "
        "publicly-known dev default, and SERMON_API_ENV is not 'dev'. "
        "Set SERMON_API_JWT_SECRET (generate with `openssl rand -hex 48`) "
        "for production, or set SERMON_API_ENV=dev for local development."
    )
    raise RuntimeError(msg)


# Hosts that mark a CORS origin as a leftover dev default — meaningless
# (or worse, attacker-registrable on a shared host) for a real deployment.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}  # noqa: S104 — denylist entry, not a bind


def _guard_cors_origins() -> None:
    """Refuse credentialed CORS with a promiscuous origin list (Phase 19).

    ``main.py`` pairs ``allow_origins`` with ``allow_credentials=True``;
    Starlette then mirrors the request Origin back for a ``"*"`` entry —
    effectively handing ANY website credentialed API access. An unset/empty
    list or a leftover loopback default outside dev is the same misconfig
    in a quieter coat (the operator never set the real origin), so all of
    them refuse boot. Reads settings at lifespan time, not import time —
    same testability contract as the JWT guard.
    """
    if settings.env == "dev":
        return
    origins = settings.cors_origins
    offenders = [
        origin
        for origin in origins
        if "*" in origin
        or not origin.strip()
        or (urlsplit(origin).hostname or "") in _LOOPBACK_HOSTS
    ]
    if origins and not offenders:
        return
    problem = (
        "the origin list is empty/unset" if not origins else f"offending entries: {offenders!r}"
    )
    msg = (
        "Refusing to start: SERMON_API_CORS_ORIGINS must list the exact "
        "production browser origin(s) — this app sends CORS responses with "
        "allow_credentials=True, and a wildcard, empty, or localhost origin "
        f"outside dev grants other sites credentialed access ({problem}). "
        'Set SERMON_API_CORS_ORIGINS (JSON list, e.g. ["https://app.example.com"]) '
        "for production, or set SERMON_API_ENV=dev for local development."
    )
    raise RuntimeError(msg)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Boot-time guards — fail loudly at startup, before the first request.

    Settings are read here rather than at import time so test collection
    (which merely imports this module under dev defaults) stays guard-free:
    uvicorn and ``with TestClient(app):`` execute the lifespan, a bare
    import does not. Phase 18: JWT-secret guard; Phase 19: CORS
    prod-origin guard.
    """
    _guard_jwt_secret()
    _guard_cors_origins()
    yield


app = FastAPI(
    title="sermon.guide API",
    description="HTTP layer — auth, upload, task status.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(MissingInferenceKeyError)
async def missing_inference_key_handler(
    _request: Request,
    exc: MissingInferenceKeyError,
) -> JSONResponse:
    """Unset DEEPINFRA_API_KEY → 503: the service is unconfigured, not broken."""
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": str(exc)},
    )


@app.exception_handler(RemoteInferenceError)
async def remote_inference_failed_handler(
    _request: Request,
    exc: RemoteInferenceError,
) -> JSONResponse:
    """Remote inference failed after retry → 502 naming the provider + leg."""
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"detail": str(exc)},
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(calendar_routes.router)
app.include_router(documents.router)
app.include_router(library.router)
app.include_router(reader.router)
app.include_router(uploads.router)
app.include_router(search.router)
app.include_router(summary.router)
app.include_router(readyz.router)


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict[str, str]:
    """Liveness probe. Cheap; does not touch DB or Redis."""
    return {"status": "ok"}
