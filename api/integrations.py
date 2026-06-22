"""OAuth integration routes — authorize, callback, list, revoke (Phase 44).

The B4 OAuth vault surface. A user connects their Google account so a later
phase (45) can pull/push sermons to Drive; this phase only mints + stores the
encrypted refresh token and surfaces the connection's identity (email). NO
google SDK — two thin ``httpx`` calls (token exchange + userinfo) per the
ADR 0005/0006 precedent.

## Security core (the phase deliverable)

Both the ``state`` HMAC and the PKCE verifier are validated BEFORE the
token-exchange POST. The validation order in :func:`callback` is strict:

  a. recompute the state HMAC over the payload and constant-time compare;
  b. ``exp`` not in the past;
  c. ``provider`` matches the path;
  d. ``user_id`` in the state equals the JWT user redeeming the callback —
     THE account-binding CSRF defense (without it, an attacker who injects
     their own state into a victim's session could bind the victim to the
     attacker's Google account);
  e. atomically pop (GETDEL) the PKCE verifier from Redis — single-use.

Only after a–e all pass does the code reach Google's token endpoint. Any
failure is a generic 400 with no oracle.

## PKCE verifier store

The verifier lives in Redis (the limiter Redis, db 2) keyed by the state
nonce — NOT a cookie. The callback runs on the api driven server-side by the
web ``/api/integrations/{provider}/callback`` route forwarding the user's
bearer; the web->api hop does not carry the browser's httpOnly cookie to the
api origin, so a web-origin cookie is unreadable here. Redis-keyed-by-nonce
is the clean cross-hop store and gives free TTL expiry. The verifier is never
in the URL, never in the browser.

## Tenant gate

Every query filters by ``current_user.user_id`` (JWT-derived), never from the
body/path/state. A cross-tenant or never-connected provider collapses to the
same byte-identical 404 with no existence oracle (the documents/calendar
``_require_owned`` posture). No token/ciphertext ever appears in a response.

## Logging

NEVER pass the authorization ``code``, ``code_verifier``, ``code_challenge``,
refresh/access tokens, ``client_secret``, or ciphertext as a structured log
key or interpolate them into a message — log only provider, user_id, and
outcome (Phase 27 doctrine; ``code`` is too generic to add to the global
deny-list, so the discipline here is the primary defense).
"""

# redis.asyncio command methods return the loosely-typed `ResponseT` union
# (the ratelimit.py / readyz.py accommodation), reported as partially Unknown
# under pyright strict.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import redis.asyncio as aioredis
import structlog
from db import OAuthConnection
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import Select, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.postgresql.dml import Insert as PgInsert
from sqlalchemy.sql.dml import ReturningDelete

import crypto_vault
from auth import CurrentUserDep, SessionDep
from crypto_vault import OAuthUnconfiguredError
from settings import settings
from tasks_client import RedisSettings

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/integrations", tags=["integrations"])

# Providers the OAuth surface currently supports. 'microsoft' is added in
# Phase 46 (config-only — the route shapes stay generic). An unconfigured /
# unknown provider is a 404, never a 500.
_ALLOWED_PROVIDERS = frozenset({"google"})

# --- Google endpoints (thin httpx; NO SDK) -----------------------------------
_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 — public endpoint URL, not a secret
_GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
_GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"  # noqa: S105 — public endpoint URL

# The granted scope string we request. Identity (email) + offline access
# (refresh token) + drive.file (for Phase 45). Space-delimited per OAuth 2.
_GOOGLE_SCOPES = "openid email profile https://www.googleapis.com/auth/drive.file"

# State lifetime — ~10 minutes. The Redis PKCE-verifier TTL matches exactly.
_STATE_TTL_SECONDS = 600

# Same logical Redis db the rate limiter uses (db 2). The PKCE verifier is an
# api-only concern; see ratelimit.LIMITER_DB.
_PKCE_DB = 2
_PKCE_KEY_PREFIX = "oauth:pkce:"  # noqa: S105 — Redis key prefix, not a credential

# Hard socket budget per Redis round trip (the ratelimit.py value).
_REDIS_OP_TIMEOUT_SECONDS = 1.0

# Hard timeout for the Google HTTP calls — a wedged Google must not hang the
# request indefinitely.
_HTTP_TIMEOUT_SECONDS = 10.0

# HTTP 200 — a plain int so the comparison against ``resp.status_code`` (also
# int) does not trip pyright's IntEnum overlap check.
_HTTP_OK = 200

# Process-wide async Redis client — lazy (import time must never need infra),
# the ratelimit._redis singleton pattern.
_redis_client: aioredis.Redis | None = None


def _redis() -> aioredis.Redis:
    global _redis_client  # noqa: PLW0603 — module-level singleton (ratelimit.py precedent)
    if _redis_client is None:
        _redis_client = aioredis.Redis.from_url(
            RedisSettings().url(_PKCE_DB),
            socket_connect_timeout=_REDIS_OP_TIMEOUT_SECONDS,
            socket_timeout=_REDIS_OP_TIMEOUT_SECONDS,
        )
    return _redis_client


# --- response models ---------------------------------------------------------


class ConnectionOut(BaseModel):
    """One listed connection. NEVER any ciphertext or token material."""

    provider: str
    provider_account_email: str
    scopes: str
    connected_at: datetime
    token_expiry: datetime | None


class ConnectionListResponse(BaseModel):
    connections: list[ConnectionOut]


class AuthorizeResponse(BaseModel):
    authorize_url: str


class CallbackResponse(BaseModel):
    provider: str
    provider_account_email: str


# --- base64url helpers -------------------------------------------------------


def _b64url(raw: bytes) -> str:
    """URL-safe base64 with padding stripped (the OAuth/PKCE encoding)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    """Inverse of :func:`_b64url` — re-pad then urlsafe-decode."""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


# --- state HMAC (mint + verify) ----------------------------------------------


def _state_secret() -> bytes:
    """The HMAC key for the OAuth ``state``.

    Prefer the dedicated ``SERMON_API_OAUTH_STATE_SECRET`` (decouples OAuth
    state forgery from session JWTs); fall back to ``jwt_secret`` when unset.
    """
    secret = settings.oauth_state_secret or settings.jwt_secret
    return secret.encode("utf-8")


def _mint_state(*, user_id: uuid.UUID, nonce: str, provider: str) -> str:
    """Mint a tamper-evident, account-bound, expiring ``state`` token.

    ``state = b64url(payload) + '.' + b64url(HMAC-SHA256(payload))`` where the
    payload is JSON ``{user_id, nonce, provider, exp}``. This binds state to
    the account (``user_id``), makes it tamper-evident (HMAC), and expiring
    (``exp`` ~10 min out).
    """
    exp = int((datetime.now(tz=UTC) + timedelta(seconds=_STATE_TTL_SECONDS)).timestamp())
    payload = json.dumps(
        {"user_id": str(user_id), "nonce": nonce, "provider": provider, "exp": exp},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    sig = hmac.new(_state_secret(), payload, hashlib.sha256).digest()
    return f"{_b64url(payload)}.{_b64url(sig)}"


class _StatePayload(BaseModel):
    user_id: str
    nonce: str
    provider: str
    exp: int


def _verify_state(state: str) -> _StatePayload:
    """Verify the state HMAC + decode the payload, or raise a generic 400.

    Recomputes the HMAC over the payload bytes and ``hmac.compare_digest``
    (constant-time) against the supplied signature. A malformed token, a
    tampered payload, or a bad signature all collapse to the same generic 400
    — no oracle. ``exp`` / ``provider`` / ``user_id`` binding are checked by
    the caller (they need request context), NOT here.
    """
    bad = HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OAuth state.")
    payload_b64, sep, sig_b64 = state.partition(".")
    if not sep:
        raise bad
    try:
        payload = _b64url_decode(payload_b64)
        sig = _b64url_decode(sig_b64)
    except (ValueError, TypeError) as exc:
        raise bad from exc
    expected = hmac.new(_state_secret(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise bad
    try:
        data = json.loads(payload)
        return _StatePayload.model_validate(data)
    except (ValueError, TypeError) as exc:
        raise bad from exc


# --- PKCE --------------------------------------------------------------------


def _make_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for an S256 PKCE exchange.

    The verifier is a high-entropy 43–128 char token; the challenge is
    ``b64url(sha256(verifier))`` with padding stripped (method=S256). The
    verifier lives ONLY server-side (Redis); only the challenge goes in the
    auth URL.
    """
    code_verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
    return code_verifier, challenge


# --- redirect URI ------------------------------------------------------------


def _redirect_uri(provider: str) -> str:
    """The operator-registered redirect URI on the WEB origin.

    Path ``/api/integrations/{provider}/callback`` on ``settings.web_origin``.
    Derived from ONE settings source so the authorize URL and the token
    exchange use a byte-identical value (a mismatch is ``redirect_uri_mismatch``
    from Google). NEVER hardcoded twice.
    """
    return f"{settings.web_origin.rstrip('/')}/api/integrations/{provider}/callback"


# --- provider config guards (validate-on-use) --------------------------------


def _require_provider(provider: str) -> None:
    """404 unless *provider* is a supported provider key (no 500 leak)."""
    if provider not in _ALLOWED_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown integration provider.",
        )


def _require_google_configured() -> tuple[str, str]:
    """Return ``(client_id, client_secret)`` or raise 503 (validate-on-use).

    Empty client id/secret means Google OAuth is unconfigured — a 503 naming
    the env var, NOT a 500 (the ``crypto_vault`` 503 posture). The detail
    names only the env var, never a value.
    """
    if not settings.google_client_id:
        msg = "Google OAuth is not configured; set SERMON_API_GOOGLE_CLIENT_ID."
        raise OAuthUnconfiguredError(msg)
    if not settings.google_client_secret:
        msg = "Google OAuth is not configured; set SERMON_API_GOOGLE_CLIENT_SECRET."
        raise OAuthUnconfiguredError(msg)
    return settings.google_client_id, settings.google_client_secret


# --- statement builders (tenant compile-pin seam) ----------------------------


def _list_stmt(user_id: uuid.UUID) -> Select[tuple[str, str, str, datetime, datetime | None]]:
    """The tenant-scoped connection list — provider/email/scopes/timestamps only.

    Factored out so the ``user_id`` filter is compile-pinned without a live DB
    (the ``library._library_stmt`` pattern). NO ciphertext columns are
    selected — the list NEVER ships token material. ``user_id`` is ALWAYS the
    JWT-derived value.
    """
    return (
        select(
            OAuthConnection.provider,
            OAuthConnection.provider_account_email,
            OAuthConnection.scopes,
            OAuthConnection.created_at,
            OAuthConnection.token_expiry,
        )
        .where(OAuthConnection.user_id == user_id)
        .order_by(OAuthConnection.created_at.desc())
    )


def _connection_stmt(user_id: uuid.UUID, provider: str) -> Select[tuple[OAuthConnection]]:
    """The single (user, provider) connection lookup — tenant-scoped.

    Scoped by ``user_id`` (ALWAYS JWT-derived) AND ``provider``. Used by the
    revoke path to fetch the refresh-token ciphertext before the row delete.
    """
    return select(OAuthConnection).where(
        OAuthConnection.user_id == user_id,
        OAuthConnection.provider == provider,
    )


def _upsert_stmt(
    *,
    user_id: uuid.UUID,
    provider: str,
    provider_account_email: str,
    refresh_token_ciphertext: bytes,
    access_token_ciphertext: bytes | None,
    token_expiry: datetime | None,
    scopes: str,
) -> PgInsert:
    """INSERT … ON CONFLICT(user_id, provider) DO UPDATE — reconnect overwrites.

    Backed by ``uq_oauth_connections_user_provider``. On conflict the row is
    overwritten in place (a reconnect yields a fresh refresh token) and
    ``updated_at`` is bumped EXPLICITLY via ``func.now()`` (the column has
    ``server_default`` but no ``onupdate`` — schema-wide convention).
    ``user_id`` is ALWAYS the JWT-derived value, never from the body/state.
    """
    stmt = pg_insert(OAuthConnection).values(
        user_id=user_id,
        provider=provider,
        provider_account_email=provider_account_email,
        refresh_token_ciphertext=refresh_token_ciphertext,
        access_token_ciphertext=access_token_ciphertext,
        token_expiry=token_expiry,
        scopes=scopes,
    )
    return stmt.on_conflict_do_update(
        constraint="uq_oauth_connections_user_provider",
        set_={
            "provider_account_email": provider_account_email,
            "refresh_token_ciphertext": refresh_token_ciphertext,
            "access_token_ciphertext": access_token_ciphertext,
            "token_expiry": token_expiry,
            "scopes": scopes,
            "updated_at": func.now(),
        },
    )


def _delete_stmt(user_id: uuid.UUID, provider: str) -> ReturningDelete[tuple[uuid.UUID]]:
    """The tenant-scoped revoke DELETE.

    Scoped by ``user_id`` (ALWAYS JWT-derived) AND ``provider``; RETURNING the
    id lets the handler tell "deleted one" from "matched nothing -> 404"
    without a prior SELECT. A cross-tenant or never-connected provider matches
    nothing and is the same byte-identical 404 (no existence oracle).
    """
    return (
        delete(OAuthConnection)
        .where(
            OAuthConnection.user_id == user_id,
            OAuthConnection.provider == provider,
        )
        .returning(OAuthConnection.id)
    )


# --- Google HTTP (thin) ------------------------------------------------------


async def _exchange_code(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    """POST the authorization code -> tokens. Raises a generic 400 on failure.

    The ``code`` / ``code_verifier`` / ``client_secret`` are sent in the POST
    body and NEVER logged. A non-200 from Google is a generic 400 (no oracle);
    the raw upstream body is never echoed.
    """
    data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.post(_GOOGLE_TOKEN_URL, data=data)
    if resp.status_code != _HTTP_OK:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth token exchange failed.",
        )
    return resp.json()


async def _fetch_email(access_token: str) -> str:
    """GET the userinfo endpoint -> the account email. Generic 400 on failure.

    The access token rides the ``Authorization`` header (never logged). A
    missing email claim or non-200 is a generic 400 (no oracle).
    """
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.get(
            _GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != _HTTP_OK:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth userinfo fetch failed.",
        )
    email = resp.json().get("email")
    if not isinstance(email, str) or not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth userinfo fetch failed.",
        )
    return email


async def _best_effort_revoke(refresh_token: str) -> None:
    """POST the refresh token to Google's revoke endpoint; swallow any failure.

    The local row delete is authoritative; a Google revoke failure (network,
    already-revoked, dead token) is logged WITHOUT the token and never raised.
    """
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            await client.post(_GOOGLE_REVOKE_URL, data={"token": refresh_token})
    except httpx.HTTPError:
        logger.warning("oauth_revoke_upstream_failed", provider="google")


# --- routes ------------------------------------------------------------------


@router.get("", response_model=ConnectionListResponse)
async def list_connections(
    current_user: CurrentUserDep,
    session: SessionDep,
) -> ConnectionListResponse:
    """List the JWT user's connections — provider/email/scopes/timestamps only.

    NEVER includes ciphertext or token material (``_list_stmt`` selects no
    token columns). Tenant-scoped to ``current_user.user_id``.
    """
    rows = (await session.execute(_list_stmt(current_user.user_id))).tuples().all()
    return ConnectionListResponse(
        connections=[
            ConnectionOut(
                provider=provider,
                provider_account_email=email,
                scopes=scopes,
                connected_at=created_at,
                token_expiry=token_expiry,
            )
            for (provider, email, scopes, created_at, token_expiry) in rows
        ],
    )


@router.post("/{provider}/authorize", response_model=AuthorizeResponse)
async def authorize(
    provider: str,
    current_user: CurrentUserDep,
) -> AuthorizeResponse:
    """Mint the Google authorization URL for the JWT user.

    404 for an unknown provider; 503 if Google/the enc-key is unconfigured
    (validate-on-use). Mints an account-bound HMAC ``state`` + an S256 PKCE
    pair, stores the verifier in Redis under the state nonce (EX == state
    TTL), and returns the auth URL with ``access_type=offline`` +
    ``prompt=consent`` (both REQUIRED to receive a refresh token, and to
    receive a FRESH one on every reconnect).
    """
    _require_provider(provider)
    client_id, _ = _require_google_configured()
    # Validate the vault key here too so an unconfigured key 503s at authorize,
    # not only at callback (validate-on-use, fail early on the user-initiated
    # step).
    crypto_vault.ensure_configured()

    nonce = secrets.token_urlsafe(32)
    state = _mint_state(user_id=current_user.user_id, nonce=nonce, provider=provider)
    code_verifier, code_challenge = _make_pkce()

    # The verifier lives ONLY server-side, keyed by the state nonce, with a
    # TTL == the state lifetime. Never in the URL, never in the browser.
    await _redis().set(f"{_PKCE_KEY_PREFIX}{nonce}", code_verifier, ex=_STATE_TTL_SECONDS)

    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(provider),
        "response_type": "code",
        "scope": _GOOGLE_SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        # REQUIRED to actually receive a refresh_token (offline) and a FRESH
        # one on every reconnect (consent) — see the risks note.
        "access_type": "offline",
        "prompt": "consent",
    }
    logger.info("oauth_authorize", provider=provider, user_id=str(current_user.user_id))
    return AuthorizeResponse(authorize_url=f"{_GOOGLE_AUTH_URL}?{urlencode(params)}")


@router.get("/{provider}/callback", response_model=CallbackResponse)
async def callback(
    provider: str,
    code: str,
    state: str,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> CallbackResponse:
    """Validate state + PKCE, exchange the code, encrypt + upsert the row.

    The validation order is strict and ALL runs BEFORE any network call to
    Google's token endpoint (a–e in the module docstring): state HMAC, exp,
    provider match, user_id account-binding, then the atomic GETDEL of the
    single-use PKCE verifier. Only then is the code exchanged, the email
    fetched, the tokens encrypted, and the row UPSERTed scoped to the JWT user
    (ON CONFLICT(user_id, provider) DO UPDATE — reconnect overwrites in
    place). Any validation failure is a generic 400 (no oracle). The ``code``
    is never logged as a structured key.
    """
    _require_provider(provider)
    client_id, client_secret = _require_google_configured()

    bad = HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OAuth state.")

    # a. HMAC + decode (constant-time compare inside).
    payload = _verify_state(state)
    # b. exp not in the past.
    if payload.exp <= int(datetime.now(tz=UTC).timestamp()):
        raise bad
    # c. provider in the state matches the path provider.
    if payload.provider != provider:
        raise bad
    # d. ACCOUNT-BINDING (the phase deliverable): the JWT user redeeming the
    #    callback MUST equal the user the state was minted for.
    if payload.user_id != str(current_user.user_id):
        raise bad
    # e. Atomic single-use pop of the PKCE verifier. Absent (expired / replayed
    #    / never-issued) -> 400. GETDEL pops it so a second redeem fails.
    raw_verifier = await _redis().getdel(f"{_PKCE_KEY_PREFIX}{payload.nonce}")
    if raw_verifier is None:
        raise bad
    code_verifier = (
        raw_verifier.decode("utf-8") if isinstance(raw_verifier, bytes) else str(raw_verifier)
    )

    # ONLY now — all of a–e passed — reach Google.
    redirect_uri = _redirect_uri(provider)
    tokens = await _exchange_code(
        code=code,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
        client_id=client_id,
        client_secret=client_secret,
    )

    refresh_token = tokens.get("refresh_token")
    access_token = tokens.get("access_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        # No refresh token (e.g. consent was not re-prompted) — we cannot
        # persist a usable connection. Generic 400, no oracle.
        raise bad
    if not isinstance(access_token, str) or not access_token:
        raise bad

    email = await _fetch_email(access_token)

    granted_scope = tokens.get("scope")
    scopes = granted_scope if isinstance(granted_scope, str) and granted_scope else _GOOGLE_SCOPES

    token_expiry: datetime | None = None
    expires_in = tokens.get("expires_in")
    if isinstance(expires_in, int):
        token_expiry = datetime.now(tz=UTC) + timedelta(seconds=expires_in)

    # Encrypt BEFORE the write — the DB never holds plaintext token material.
    refresh_ct = crypto_vault.encrypt(refresh_token)
    access_ct = crypto_vault.encrypt(access_token)

    await session.execute(
        _upsert_stmt(
            user_id=current_user.user_id,
            provider=provider,
            provider_account_email=email,
            refresh_token_ciphertext=refresh_ct,
            access_token_ciphertext=access_ct,
            token_expiry=token_expiry,
            scopes=scopes,
        ),
    )
    await session.commit()
    logger.info("oauth_callback_connected", provider=provider, user_id=str(current_user.user_id))
    return CallbackResponse(provider=provider, provider_account_email=email)


@router.delete("/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke(
    provider: str,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> None:
    """Disconnect the (JWT user, provider) connection. 404 (no oracle) otherwise.

    Best-effort POST to Google's revoke endpoint with the decrypted refresh
    token (failure is swallowed/logged — the local delete is authoritative),
    then DELETE the row scoped to ``current_user.user_id`` AND ``provider``. A
    cross-tenant or never-connected provider matches nothing and is the same
    byte-identical 404. 204 on success.
    """
    _require_provider(provider)
    not_found = HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Integration not connected.",
    )
    # Load the row first (tenant-scoped) so we can best-effort revoke the
    # decrypted refresh token before deleting it.
    connection = (
        await session.execute(_connection_stmt(current_user.user_id, provider))
    ).scalar_one_or_none()
    if connection is None:
        raise not_found
    # Decrypt + best-effort revoke. A tamper/InvalidTag surfaces as a 500 (no
    # oracle); a Google-side failure is swallowed.
    refresh_token = crypto_vault.decrypt(connection.refresh_token_ciphertext)
    await _best_effort_revoke(refresh_token)

    result = await session.execute(_delete_stmt(current_user.user_id, provider))
    if result.scalar_one_or_none() is None:
        # Raced away between the SELECT and DELETE — same 404 shape.
        raise not_found
    await session.commit()
    logger.info("oauth_revoked", provider=provider, user_id=str(current_user.user_id))
