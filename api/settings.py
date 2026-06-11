"""HTTP-layer settings loaded from ``SERMON_API_*`` env vars.

Auth secrets, upload paths, and CORS origins live here so callers don't
have to thread env-var literals through the codebase. ``infra/.env``
carries the local-dev defaults; Make targets source it before invoking
uvicorn (see ``api/Makefile``).

The JWT secret defaults to ``DEV_JWT_SECRET`` — a publicly-known
placeholder (it lives in this repo). ``main.py``'s lifespan guard
(Phase 18) refuses to boot while it is in effect — unset/empty or still
the placeholder — unless ``SERMON_API_ENV=dev`` explicitly opts into
local-dev mode. Production must set ``SERMON_API_JWT_SECRET``; the prod
compose additionally hard-fails at compose-up without it
(``infra/docker-compose.prod.yml``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The local-dev JWT signing secret. Publicly known, so signing tokens with it
# is equivalent to no auth at all — anyone could mint a valid JWT for any
# user_id (total tenant-isolation defeat). One constant serves as both the
# field default and the boot guard's comparand (``main.py``) so the two can
# never drift.
DEV_JWT_SECRET = "change-me-in-production-this-is-local-dev-only"  # noqa: S105 — dev placeholder; boot-guarded


def parse_rate(value: str) -> tuple[int, int]:
    """Parse a ``"<max requests>/<window seconds>"`` rate string, e.g. ``"10/60"``.

    Single source of truth for the rate-limit bucket format (Phase 19):
    the field validator below rejects malformed env values at process
    start with a self-diagnosing error, and ``ratelimit.py`` re-parses
    the validated value at request time.
    """
    limit_raw, sep, window_raw = value.partition("/")
    try:
        limit, window = int(limit_raw), int(window_raw)
    except ValueError:
        sep = ""  # fall through to the shared error below
        limit = window = 0
    if not sep or limit < 1 or window < 1:
        msg = (
            f"Invalid rate limit {value!r}: expected '<max requests>/<window seconds>' "
            "with both positive integers, e.g. '10/60'."
        )
        raise ValueError(msg)
    return limit, window


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SERMON_API_", extra="ignore")

    # Deployment posture (SERMON_API_ENV). Default "prod" = fail closed: any
    # environment that does not explicitly declare itself dev gets the full
    # boot guards in ``main.py:lifespan`` (Phase 18 JWT-secret guard; Phase
    # 19 CORS prod-origin guard). ``make dev`` sources ``infra/.env``, which
    # sets SERMON_API_ENV=dev — never set "dev" on a deployment that faces
    # real users.
    env: Literal["dev", "prod"] = "prod"

    jwt_secret: str = DEV_JWT_SECRET
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

    # Must list the exact prod browser origin(s) outside dev — the lifespan
    # guard in ``main.py`` (Phase 19) refuses to boot a non-dev process whose
    # list is empty or contains a wildcard/empty/localhost origin, because
    # ``main.py`` pairs it with ``allow_credentials=True``.
    cors_origins: list[str] = ["http://localhost:3000"]

    # Phase 19 — client-IP trust for rate limiting. When True, the per-IP
    # limiter keys on the first X-Forwarded-For entry instead of the TCP
    # peer. ONLY safe when every network path to this process goes through
    # a proxy that sets the header itself (prod compose: Caddy discards
    # client-supplied XFF and writes the real peer; the web proxy forwards
    # it). Default False = fail closed to the TCP peer address — never
    # enable where clients can reach :8000 directly.
    trust_proxy_headers: bool = False

    # Phase 19 — Redis-backed rate-limit buckets, "<max requests>/<window
    # seconds>" per named bucket (see api/AGENTS.md for the table and
    # ``ratelimit.py`` for the mechanics). Adding a bucket = one field here
    # (named ``ratelimit_<bucket>``) + one route dependency. ``enabled`` is
    # an operational kill switch (false-positive lockouts); Redis-down
    # already fails open at request time.
    ratelimit_enabled: bool = True
    ratelimit_signup_ip: str = "5/60"
    ratelimit_login_ip: str = "10/60"
    ratelimit_summary_user: str = "5/60"

    # LLM summary agent (Phase 14, transport re-cut in Phase 14b / ADR 0005;
    # ``deepinfra`` provider added Phase 16b / ADR 0006). ``llm_provider`` picks
    # which OpenAI-compatible endpoint /search-summary talks to; the
    # per-provider base_url / default model / key live in ``summary.py:_PROVIDERS``
    # (single source of truth). ``deepinfra`` reuses DEEPINFRA_API_KEY so the
    # whole platform — embeddings, rerank, highlight, AND the summary LLM — can
    # run on one vendor + one key.
    llm_provider: Literal["google", "ppq", "deepinfra"] = "google"

    # Optional model-id override (SERMON_API_LLM_MODEL); ``None`` → the active
    # provider's default. Spell it the provider's way — bare ``gemini-2.5-flash``
    # on google, prefixed ``google/gemini-2.5-flash`` on ppq/deepinfra.
    llm_model: str | None = None

    # Optional reasoning-effort knob (SERMON_API_LLM_REASONING_EFFORT); ``None``
    # → not sent, provider default applies. Phase 16b latency lever: Gemini 2.5
    # Flash runs thinking by default through the OpenAI-compat layer (~60s of
    # the /search-summary round-trip); Google's compat endpoint accepts
    # ``"none"`` to disable it. Sent verbatim via ``extra_body`` — whether a
    # gateway (ppq) forwards it is a provider property, probed live per phase
    # row, not assumed.
    llm_reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = None

    @field_validator("env", mode="before")
    @classmethod
    def _empty_env_is_prod(cls, value: object) -> object:
        """Compose's ``${VAR:-}`` pattern delivers ``""`` for unset — fail closed.

        An empty SERMON_API_ENV must mean "prod posture" (guards armed),
        not a Literal validation error and not an accidental dev opt-out.
        """
        return "prod" if value == "" else value

    @field_validator("ratelimit_signup_ip", "ratelimit_login_ip", "ratelimit_summary_user")
    @classmethod
    def _rate_strings_must_parse(cls, value: str) -> str:
        """A malformed bucket must fail at process start, not at request time."""
        parse_rate(value)  # raises ValueError with a self-diagnosing message
        return value

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
    # Phase 16b: the same key the embeddings/rerank/highlight legs use
    # (worker/inference.py). When ``llm_provider=deepinfra`` the summary LLM
    # rides DeepInfra's OpenAI-compatible chat endpoint too — one vendor, one
    # key for the whole inference stack.
    deepinfra_api_key: str | None = Field(default=None, validation_alias="DEEPINFRA_API_KEY")


settings = ApiSettings()
