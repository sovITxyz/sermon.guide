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

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from inference import MissingInferenceKeyError, RemoteInferenceError

import auth
import library
import search
import summary
import uploads
from settings import settings

app = FastAPI(
    title="sermon.guide API",
    description="HTTP layer — auth, upload, task status.",
    version="0.1.0",
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
app.include_router(library.router)
app.include_router(uploads.router)
app.include_router(search.router)
app.include_router(summary.router)


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict[str, str]:
    """Liveness probe. Cheap; does not touch DB or Redis."""
    return {"status": "ok"}
