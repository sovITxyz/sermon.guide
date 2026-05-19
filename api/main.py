"""FastAPI application entrypoint — health, CORS, route mounting.

Run for local dev with ``make dev`` (uvicorn --reload). Production
deployment is out of scope for Phase 10 — k8s manifests land alongside
KEDA in a later phase.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import auth
import uploads
from settings import settings

app = FastAPI(
    title="sermon.guide API",
    description="HTTP layer — auth, upload, task status.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(uploads.router)


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict[str, str]:
    """Liveness probe. Cheap; does not touch DB or Redis."""
    return {"status": "ok"}
