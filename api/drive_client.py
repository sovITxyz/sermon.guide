"""Thin Google Drive REST client + access-token provider (Phase 45).

The api-side Drive surface backing the editor-link round-trip. NO google SDK —
thin ``httpx`` calls to FIXED Google endpoints (the ADR 0005/0006 precedent;
mirrors ``integrations.py``'s ``_exchange_code`` shape). The worker never
touches OAuth or Drive, so this lives beside ``crypto_vault.py`` (api-only).

## Two parts

1. **Access-token provider** (:func:`get_access_token`) — reuse the cached
   access token from the Phase 44 ``oauth_connections`` row when it is still
   valid (decrypt + return, no network), else REFRESH via the stored refresh
   token, re-encrypt the new access token, and persist it back on the SAME
   row (tenant-scoped). A revoked / expired refresh token (Google's
   ``invalid_grant`` — the 7-day Testing-mode expiry) raises
   :class:`DriveAuthError` so the route can flip the editor-link row to
   ``state='error'`` and surface a re-connect prompt rather than a 500.

2. **Drive REST calls** — upload-with-conversion (export a ``.docx`` to a
   NATIVE Google Doc), get the ``webViewLink``, get the ``version`` cursor,
   export ``text/markdown`` (the pull leg), and best-effort delete. The
   endpoints are CONSTANT; ``provider_file_id`` is path-segment-encoded but is
   never used to assemble an attacker-controlled URL (the SSRF guard — the
   route only ever passes the file id from the user's OWN editor-link row). The
   ``drive.file`` scope (granted at connect time, ``integrations._GOOGLE_SCOPES``)
   governs app-created files — sufficient for create/get/export/delete.

## Logging

NEVER pass an access token, refresh token, ciphertext, or ``client_secret`` as
a structured log key or interpolate it into a message — log only provider /
user_id / outcome (the Phase 27 doctrine; the ``integrations.py`` precedent).
A non-200 from Drive is a :class:`DriveError` the route maps to 502 — never an
oracle, the raw upstream body is never echoed.
"""

# Reuses ``integrations._require_google_configured`` (the SAME client id/secret
# guard the OAuth surface uses) per the Phase 45 plan rather than re-deriving
# it — keep ``reportPrivateUsage`` off for that single sanctioned reuse.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import quote

import httpx
import structlog
from db import OAuthConnection
from sqlalchemy import Update, func, update
from sqlalchemy.ext.asyncio import AsyncSession

import crypto_vault
from integrations import _require_google_configured

logger = structlog.get_logger(__name__)

# --- Google / Drive endpoints (thin httpx; NO SDK; CONSTANT) -----------------
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 — public endpoint URL, not a secret
_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
_DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"

# The native Google Docs mime — uploading a .docx with this target mimeType
# triggers Drive's upload-with-conversion to a real editable Doc.
_GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
# The .docx wire mime (the media part's content-type). Kept byte-identical with
# documents._DOCX_MIME — a single value across the upload + the export header.
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Hard socket budget per Drive / token round trip (the integrations.py value).
_HTTP_TIMEOUT_SECONDS = 10.0

# HTTP 200 — a plain int so the comparison against ``resp.status_code`` (also
# int) does not trip pyright's IntEnum overlap check.
_HTTP_OK = 200

# Refresh slightly BEFORE the stored access token actually expires so an
# in-flight request never races the boundary (clock skew + request latency).
_EXPIRY_SKEW_SECONDS = 60


class DriveError(RuntimeError):
    """A Drive REST call (or the token refresh) failed at the upstream.

    Raised instead of leaking a raw httpx error / upstream body so the route
    maps it to a clean 502 with a fixed detail — never a stack-trace or
    upstream-body oracle. NEVER carries token material.
    """


class DriveAuthError(DriveError):
    """The stored refresh token is revoked / expired (Google ``invalid_grant``).

    A distinct subtype so the route can flip the editor-link row to
    ``state='error'`` and surface a re-connect prompt, rather than a generic
    502. This is the expected outcome once a Testing-mode refresh token hits
    its 7-day expiry.
    """


def _access_token_valid(connection: OAuthConnection) -> bool:
    """True if the row's cached access token is present AND not near expiry.

    Reuses the Phase 44 row's ``access_token_ciphertext`` + ``token_expiry``
    (stored exactly to avoid a refresh round-trip). A ``_EXPIRY_SKEW_SECONDS``
    margin means we refresh slightly early so an in-flight call never races the
    boundary. A missing ciphertext or expiry, or an expiry within the skew
    window, returns False (force a refresh).
    """
    if connection.access_token_ciphertext is None or connection.token_expiry is None:
        return False
    expiry = connection.token_expiry
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry > datetime.now(tz=UTC) + timedelta(seconds=_EXPIRY_SKEW_SECONDS)


def _persist_access_token_stmt(
    *,
    connection_id: uuid.UUID,
    user_id: uuid.UUID,
    access_token_ciphertext: bytes,
    token_expiry: datetime,
) -> Update:
    """Build the tenant-scoped UPDATE that persists a refreshed access token.

    Scoped by the row's ``id`` AND ``user_id`` (the JWT-derived owner, NEVER a
    body/path value) so a refresh can only ever rewrite the caller's OWN
    connection row. ``updated_at`` is bumped EXPLICITLY via ``func.now()`` (the
    column has ``server_default`` but no ``onupdate`` — the schema-wide
    convention). Only the access-token ciphertext + expiry change; the refresh
    token is untouched.
    """
    return (
        update(OAuthConnection)
        .where(
            OAuthConnection.id == connection_id,
            OAuthConnection.user_id == user_id,
        )
        .values(
            access_token_ciphertext=access_token_ciphertext,
            token_expiry=token_expiry,
            updated_at=func.now(),
        )
    )


async def get_access_token(connection: OAuthConnection, session: AsyncSession) -> str:
    """Return a usable Google access token for *connection* (cache, else refresh).

    If the cached access token is still valid (:func:`_access_token_valid`),
    decrypt and RETURN it — no network round-trip. Otherwise REFRESH: decrypt
    the refresh token, POST it to Google's token endpoint, re-encrypt the new
    access token, and persist the fresh ciphertext + expiry back on the SAME
    row (tenant-scoped UPDATE by ``id`` + ``user_id``), then return the
    plaintext token (in-memory ONLY — never logged, never returned to a
    response).

    Google ``invalid_grant`` (the refresh token revoked / past its Testing-mode
    7-day expiry) raises :class:`DriveAuthError`; any other non-200 raises
    :class:`DriveError`. The token NEVER appears in a log line or message.
    """
    if _access_token_valid(connection):
        assert connection.access_token_ciphertext is not None  # noqa: S101 — _access_token_valid pins it
        return crypto_vault.decrypt(connection.access_token_ciphertext)

    client_id, client_secret = _require_google_configured()
    refresh_token = crypto_vault.decrypt(connection.refresh_token_ciphertext)
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(_GOOGLE_TOKEN_URL, data=data)
    except httpx.HTTPError as exc:
        logger.warning(
            "drive_token_refresh_failed",
            provider=connection.provider,
            user_id=str(connection.user_id),
        )
        msg = "Google token refresh failed."
        raise DriveError(msg) from exc

    if resp.status_code != _HTTP_OK:
        # ``invalid_grant`` == the refresh token is revoked / expired. Distinct
        # from a transient upstream failure so the route can flip to
        # state='error' + prompt a re-connect (never a token oracle).
        is_invalid_grant = False
        try:
            is_invalid_grant = resp.json().get("error") == "invalid_grant"
        except ValueError:
            is_invalid_grant = False
        logger.warning(
            "drive_token_refresh_rejected",
            provider=connection.provider,
            user_id=str(connection.user_id),
            invalid_grant=is_invalid_grant,
        )
        if is_invalid_grant:
            msg = "Google refresh token is no longer valid; reconnect required."
            raise DriveAuthError(msg)
        msg = "Google token refresh failed."
        raise DriveError(msg)

    body = resp.json()
    access_token = body.get("access_token")
    expires_in = body.get("expires_in")
    if not isinstance(access_token, str) or not access_token:
        msg = "Google token refresh returned no access token."
        raise DriveError(msg)
    token_expiry = datetime.now(tz=UTC) + timedelta(
        seconds=expires_in if isinstance(expires_in, int) else 0,
    )

    # Re-encrypt + persist on the SAME row (tenant-scoped). The DB never holds
    # plaintext token material.
    await session.execute(
        _persist_access_token_stmt(
            connection_id=connection.id,
            user_id=connection.user_id,
            access_token_ciphertext=crypto_vault.encrypt(access_token),
            token_expiry=token_expiry,
        ),
    )
    await session.commit()
    logger.info(
        "drive_token_refreshed",
        provider=connection.provider,
        user_id=str(connection.user_id),
    )
    return access_token


def _auth_headers(access_token: str) -> dict[str, str]:
    """The Bearer auth header. The token rides here and is NEVER logged."""
    return {"Authorization": f"Bearer {access_token}"}


def _file_path(file_id: str) -> str:
    """Path-segment-encode *file_id* into the FIXED ``files/{id}`` endpoint.

    The endpoint host/path is CONSTANT; only the single ``{file_id}`` segment
    is interpolated, and it is percent-encoded with an empty ``safe`` set so no
    ``/``, ``?``, or ``#`` can escape the segment and reshape the URL (the SSRF
    guard — combined with the route only ever passing the file id from the
    user's OWN editor-link row).
    """
    return f"{_DRIVE_FILES_URL}/{quote(file_id, safe='')}"


def _build_related_multipart(name: str, docx_bytes: bytes) -> tuple[bytes, str]:
    """Build a ``multipart/related`` body for Drive's ``uploadType=multipart``.

    Drive's multipart upload requires ``multipart/related`` (NOT
    ``multipart/form-data`` — httpx's ``files=`` would produce the latter, which
    Drive rejects): a JSON metadata part requesting ``mimeType`` =
    ``application/vnd.google-apps.document`` (so the docx is converted to a
    native Doc) followed by the docx media part. Returns ``(body, content_type)``
    where ``content_type`` carries the boundary. The boundary is random so it
    can never collide with the docx bytes.
    """
    boundary = f"sermonguide-{uuid.uuid4().hex}"
    metadata = json.dumps({"name": name, "mimeType": _GOOGLE_DOC_MIME})
    crlf = b"\r\n"
    parts = [
        f"--{boundary}".encode(),
        b"Content-Type: application/json; charset=UTF-8",
        b"",
        metadata.encode("utf-8"),
        f"--{boundary}".encode(),
        f"Content-Type: {_DOCX_MIME}".encode(),
        b"",
    ]
    body = crlf.join(parts) + crlf + docx_bytes + crlf + f"--{boundary}--".encode() + crlf
    return body, f"multipart/related; boundary={boundary}"


async def upload_with_conversion(access_token: str, name: str, docx_bytes: bytes) -> str:
    """Upload *docx_bytes* as a NATIVE Google Doc; return the created file id.

    A ``multipart/related`` upload whose metadata part requests ``mimeType`` =
    ``application/vnd.google-apps.document`` so Drive converts the ``.docx``
    media part into a real editable Doc. Returns the new file's id. A non-200
    raises :class:`DriveError` (-> route 502). The endpoint is CONSTANT.
    """
    body, content_type = _build_related_multipart(name, docx_bytes)
    headers = {**_auth_headers(access_token), "Content-Type": content_type}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                _DRIVE_UPLOAD_URL,
                params={"uploadType": "multipart"},
                headers=headers,
                content=body,
            )
    except httpx.HTTPError as exc:
        msg = "Drive upload failed."
        raise DriveError(msg) from exc
    if resp.status_code != _HTTP_OK:
        msg = "Drive upload failed."
        raise DriveError(msg)
    file_id = resp.json().get("id")
    if not isinstance(file_id, str) or not file_id:
        msg = "Drive upload returned no file id."
        raise DriveError(msg)
    return file_id


async def _files_get(access_token: str, file_id: str, *, fields: str) -> dict[str, Any]:
    """GET ``files/{id}?fields=...`` -> the JSON body. 502 on non-200."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                _file_path(file_id),
                params={"fields": fields},
                headers=_auth_headers(access_token),
            )
    except httpx.HTTPError as exc:
        msg = "Drive metadata fetch failed."
        raise DriveError(msg) from exc
    if resp.status_code != _HTTP_OK:
        msg = "Drive metadata fetch failed."
        raise DriveError(msg)
    body: Any = resp.json()
    if not isinstance(body, dict):
        msg = "Drive metadata fetch returned an unexpected body."
        raise DriveError(msg)
    return cast("dict[str, Any]", body)


async def get_web_view_link(access_token: str, file_id: str) -> str:
    """Return the Doc's ``webViewLink`` (the in-browser open URL). 502 on failure."""
    body = await _files_get(access_token, file_id, fields="webViewLink")
    web_url = body.get("webViewLink")
    if not isinstance(web_url, str) or not web_url:
        msg = "Drive file has no webViewLink."
        raise DriveError(msg)
    return web_url


async def get_version(access_token: str, file_id: str) -> str:
    """Return the Doc's ``version`` cursor (a string; COMPARED, never parsed).

    Used to detect a remote edit: ``version != last_remote_version`` means the
    user changed the Doc since the last sync. The value is opaque — equality
    only, never ordered or arithmetic'd. 502 on failure.
    """
    body = await _files_get(access_token, file_id, fields="version,modifiedTime")
    version = body.get("version")
    if version is None:
        msg = "Drive file has no version."
        raise DriveError(msg)
    # Drive returns ``version`` as a JSON number-string; normalize to str so the
    # equality compare against the stored cursor is type-stable.
    return str(version)


async def export_markdown(access_token: str, file_id: str) -> str:
    """Export the Doc as ``text/markdown`` (the pull leg's source). 502 on failure.

    The returned markdown still carries Google's ``http:///read/`` citation
    form — the caller normalizes it via ``worker.convert.normalize_read_hrefs``
    (inside ``convert_from_markdown``) before re-import. The endpoint is
    CONSTANT (``files/{id}/export``).
    """
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{_file_path(file_id)}/export",
                params={"mimeType": "text/markdown"},
                headers=_auth_headers(access_token),
            )
    except httpx.HTTPError as exc:
        msg = "Drive export failed."
        raise DriveError(msg) from exc
    if resp.status_code != _HTTP_OK:
        msg = "Drive export failed."
        raise DriveError(msg)
    return resp.text


async def delete_file(access_token: str, file_id: str) -> None:
    """Best-effort DELETE of the app-created Doc on unlink. Swallows any failure.

    The local editor-link state change is authoritative; a Drive delete failure
    (network, already-trashed, dead token) is logged WITHOUT the token / file
    contents and never raised (the ``integrations._best_effort_revoke``
    posture). Only ever called on the app-created Doc id from the user's own
    row.
    """
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            await client.delete(_file_path(file_id), headers=_auth_headers(access_token))
    except httpx.HTTPError:
        logger.warning("drive_delete_best_effort_failed", provider="google")
