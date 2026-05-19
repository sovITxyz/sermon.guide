"""HTTP-layer settings loaded from ``SERMON_API_*`` env vars.

Auth secrets, upload paths, and CORS origins live here so callers don't
have to thread env-var literals through the codebase. ``infra/.env``
carries the local-dev defaults; Make targets source it before invoking
uvicorn (see ``api/Makefile``).

The JWT secret default is a placeholder marked ``noqa: S105`` and is
overridden in production via ``SERMON_API_JWT_SECRET`` — a startup-time
assertion would also catch a forgotten override, but pydantic's required
field semantics serve the same purpose if the deployment unsets the
default. Keep it required-in-spirit: never ship the default to prod.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SERMON_API_", extra="ignore")

    jwt_secret: str = "change-me-in-production-this-is-local-dev-only"  # noqa: S105
    jwt_algorithm: str = "HS256"
    # 1 hour token lifetime — long enough for a typical upload flow, short
    # enough that a leaked token's blast radius is bounded. Refresh-token
    # plumbing is out of scope for Phase 10.
    jwt_ttl_seconds: int = 3600

    # Where multipart uploads land before the Celery worker reads them.
    # R2/B2 lands in Phase 14+. Until then this is a shared filesystem path
    # both api/ and worker/ can see (compose mounts it the same way in dev).
    upload_dir: Path = Path("/tmp/sermon-uploads")  # noqa: S108

    # 200MB upload cap. Large enough for most ebooks, small enough to keep
    # a single misbehaving client from filling /tmp.
    upload_max_bytes: int = 200 * 1024 * 1024

    cors_origins: list[str] = ["http://localhost:3000"]


settings = ApiSettings()
