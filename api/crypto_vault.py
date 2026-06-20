"""AES-256-GCM token vault (Phase 44).

The api-side encrypt/decrypt primitives backing ``oauth_connections``. OAuth
refresh tokens (and the optional short-lived access token) are NEVER stored
in plaintext: ``api/integrations.py`` calls :func:`encrypt` before the UPSERT
and :func:`decrypt` only when a token is actually needed (the Phase 45 Drive
calls, or the best-effort revoke). The worker never touches OAuth, so this
lives beside ``settings.py`` / ``ratelimit.py`` (api-only), NOT in
``worker/db``.

## Key

The 256-bit key is loaded from ``SERMON_API_TOKEN_ENC_KEY`` — 64 hex chars
(``openssl rand -hex 32``), hex-decoded to 32 raw bytes. The key is validated
ON USE, never at boot: :func:`_load_key` raises :class:`OAuthUnconfiguredError`
(mapped to 503 in ``main.py``, naming the env var) when the value is
empty/missing/not-64-hex/not-decodable, so the app still boots without Google
configured (mirroring the ``/search-summary`` ``MissingInferenceKeyError`` ->
503 posture).

## Layout

:func:`encrypt` draws a fresh random 96-bit nonce per call
(``os.urandom(12)``) — NEVER reused with the same key (the GCM invariant) —
and returns ``nonce || ciphertext+tag`` (the 16-byte GCM tag is appended by
the library inside the ciphertext). :func:`decrypt` splits the first 12 bytes
back off as the nonce. A tampered or truncated blob raises
``cryptography.exceptions.InvalidTag``, which the route lets surface as a 500
— never a detail oracle, never a "decryption failed because…" message.

No associated data (AAD) is used: the row's ``user_id`` is already the tenant
gate at the query layer. AAD is an optional future hardening.

NEVER pass plaintext token material, ciphertext, or the key as a structured
log key or interpolate it into a message — log only provider / user_id /
outcome (the Phase 27 redaction doctrine; ``token`` substrings are scrubbed,
but the discipline is the primary defense).
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from settings import settings

# The env var name surfaced in the 503 detail. A module constant so the
# message and the documentation can never drift.
_ENC_KEY_ENV = "SERMON_API_TOKEN_ENC_KEY"

# AES-256 → a 32-byte key, encoded as 64 hex chars.
_KEY_BYTES = 32

# AESGCM standard nonce length (96 bits). Prepended to the ciphertext.
_NONCE_BYTES = 12


class OAuthUnconfiguredError(RuntimeError):
    """OAuth/token-vault config is missing or malformed (mapped to 503).

    Raised ON USE (never at boot) so the app still starts when Google is
    unconfigured. ``main.py`` maps this to a 503 whose detail names the
    missing/malformed env var but never any value. Used both for the token
    encryption key here and the missing Google client id/secret in
    ``integrations.py``.
    """


def _load_key() -> bytes:
    """Hex-decode ``SERMON_API_TOKEN_ENC_KEY`` to 32 raw bytes; 503 if bad.

    Validate-on-use: an empty / non-64-hex / non-decodable value raises
    :class:`OAuthUnconfiguredError` (-> 503 naming the env var). The detail
    names ONLY the env var, never the value. Returns the raw 32-byte key.
    """
    raw = settings.token_enc_key
    if not raw:
        msg = f"Token vault is not configured; set {_ENC_KEY_ENV} (`openssl rand -hex 32`)."
        raise OAuthUnconfiguredError(msg)
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        msg = f"{_ENC_KEY_ENV} must be {_KEY_BYTES * 2} hex chars (`openssl rand -hex 32`)."
        raise OAuthUnconfiguredError(msg) from exc
    if len(key) != _KEY_BYTES:
        msg = f"{_ENC_KEY_ENV} must decode to {_KEY_BYTES} bytes (AES-256); got {len(key)}."
        raise OAuthUnconfiguredError(msg)
    return key


def ensure_configured() -> None:
    """Raise :class:`OAuthUnconfiguredError` (-> 503) unless the vault key is valid.

    The public validate-on-use guard for callers (``integrations.authorize``)
    that want to fail early on a missing/malformed key BEFORE doing other work,
    without performing an actual encrypt. Discards the loaded key.
    """
    _load_key()


def encrypt(plaintext: str) -> bytes:
    """Encrypt *plaintext* under AES-256-GCM; return ``nonce || ciphertext+tag``.

    A fresh random 96-bit nonce is drawn per call (the GCM invariant: never
    reuse a nonce with the same key) and PREPENDED to the library output (the
    16-byte tag is already appended inside ``ct``). Raises
    :class:`OAuthUnconfiguredError` (-> 503) if the key is missing/malformed.
    """
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(_load_key()).encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + ct


def decrypt(blob: bytes) -> str:
    """Decrypt a ``nonce || ciphertext+tag`` *blob* back to its plaintext.

    Splits the first 12 bytes as the nonce. A tampered / truncated blob raises
    ``cryptography.exceptions.InvalidTag`` (the route lets it surface as a 500,
    never a detail oracle). Raises :class:`OAuthUnconfiguredError` (-> 503) if
    the key is missing/malformed.
    """
    nonce, ct = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    return AESGCM(_load_key()).decrypt(nonce, ct, None).decode("utf-8")
