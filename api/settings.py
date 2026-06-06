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
from typing import Literal

from pydantic import Field, field_validator
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

    # LLM summary agent (Phase 14, transport re-cut in Phase 14b / ADR 0005).
    # ``llm_provider`` picks which OpenAI-compatible endpoint /search-summary
    # talks to; the per-provider base_url / default model / key live in
    # ``summary.py:_PROVIDERS`` (single source of truth).
    llm_provider: Literal["google", "ppq"] = "google"

    # Optional model-id override (SERMON_API_LLM_MODEL); ``None`` → the active
    # provider's default. Spell it the provider's way — bare ``gemini-2.5-flash``
    # on google, prefixed ``google/gemini-2.5-flash`` on ppq.
    llm_model: str | None = None

    # Optional reasoning-effort knob (SERMON_API_LLM_REASONING_EFFORT); ``None``
    # → not sent, provider default applies. Phase 16b latency lever: Gemini 2.5
    # Flash runs thinking by default through the OpenAI-compat layer (~60s of
    # the /search-summary round-trip); Google's compat endpoint accepts
    # ``"none"`` to disable it. Sent verbatim via ``extra_body`` — whether a
    # gateway (ppq) forwards it is a provider property, probed live per phase
    # row, not assumed.
    llm_reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = None

    @field_validator("llm_reasoning_effort", mode="before")
    @classmethod
    def _empty_reasoning_effort_is_unset(cls, value: object) -> object:
        """Compose's ``${VAR:-}`` pattern delivers ``""`` for unset — treat as None.

        Same pattern the prod compose uses for SERMON_API_LLM_MODEL; without
        this, an empty env var would fail the Literal validation at boot.
        """
        return None if value == "" else value

    # Both keys are read *unprefixed* via an explicit ``validation_alias`` that
    # bypasses the ``SERMON_API_`` prefix above: GOOGLE_API_KEY is the name
    # Google's docs and SDKs use, PPQ_API_KEY is the literal name ppq.ai's docs
    # use (and both match infra/.env.example). ``None`` until configured — the
    # /search-summary route raises a clear 503 naming the missing var rather
    # than letting an unconfigured key surface as an opaque SDK error.
    google_api_key: str | None = Field(default=None, validation_alias="GOOGLE_API_KEY")
    ppq_api_key: str | None = Field(default=None, validation_alias="PPQ_API_KEY")


settings = ApiSettings()
