"""External-editor link routes — Google-Docs round-trip (Phase 45 — B4).

Mounts on the ``documents`` resource (paths ``/documents/{document_id}/
editor-link/*``) — the same resource the docx round-trip uses (the api has no
``/sermons`` prefix; the web ``/sermons`` editor proxies here). Four routes:

- ``POST   /documents/{document_id}/editor-link``         — LINK: export the
  canonical ``content`` to a NATIVE Google Doc, store one ``editor_links`` row.
- ``GET    /documents/{document_id}/editor-link/status``  — STATUS: is the Doc
  changed remotely since the last sync (``version`` cursor compare)?
- ``POST   /documents/{document_id}/editor-link/pull``    — PULL: re-import the
  Doc's markdown export into ``content`` (snapshot-first, never destructive).
- ``POST   /documents/{document_id}/editor-link/unlink``  — UNLINK: ``pull-final``
  (snapshot+overwrite once, then detach) or ``keep-app`` (detach, leave content).

## Tenant gate (load-bearing)

EVERY route is JWT-scoped via ``CurrentUserDep``. ``document_id`` AND
``provider_file_id`` are UNTRUSTED. The owned-document gate runs FIRST
(``documents._require_owned_document``): a non-owned / nonexistent / non-UUID /
soft-deleted ``document_id`` is a byte-identical 404 ``"Document not found."``
with no existence oracle. The ``editor_links`` row is ALWAYS fetched scoped to
``current_user.user_id`` AND ``document_id`` (the denormalized tenant gate) —
the route NEVER trusts a body-supplied file id as a capability. The Drive file
id used in every Google call comes ONLY from the user's own row, into FIXED
endpoints (the SSRF guard, ``drive_client``).

Every statement is factored into a module-level ``_xxx_stmt`` builder so the
``user_id`` scoping is compile-pinned in ``tests/test_editor_links_unit.py``
without a live DB (the ``documents``/``library`` ``_xxx_stmt`` seam). Request
models set ``extra="forbid"`` (Phase 18): a smuggled field is a hard 422.

## The DOCX-pull trap (do NOT "add a docx fallback")

Pull uses the ``text/markdown`` export ONLY. Google's docx conversion turns the
relative ``/read`` citation href into ``about:blank`` (unrecoverable), so
markdown is the primary AND only pull leg (the settled spike). The markdown
carries Google's ``http:///read/`` form, which ``worker.convert`` normalizes
back to ``/read/`` before re-import (else the citation node is dropped).

## Refresh-token expiry

A Testing-mode Google refresh token expires in ~7 days. A status/pull after
expiry surfaces ``DriveAuthError`` -> the row flips to ``state='error'`` and the
route returns a re-connect signal rather than a 500.
"""

# This module deliberately reuses the sibling-module gate/stmt helpers the
# Phase 45 plan mandates — ``documents._require_owned_document`` /
# ``_require_content_within_cap`` / ``_revision_insert_stmt`` / ``_update_stmt``
# / ``_REVISION_SOURCE_PULL`` and ``integrations._require_google_configured``
# (via ``drive_client``) — so the owned-document 404-no-oracle gate, the
# snapshot-first revision insert, and the content cap stay byte-identical with
# the docx round-trip rather than being re-implemented (and drifting). The
# ``_xxx`` naming is the codebase's intentional convention; suppress
# ``reportPrivateUsage`` for these sanctioned cross-module reuses only.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import uuid
from typing import Literal

import structlog
from convert import (
    ConversionError,
    convert_from_markdown,
    convert_to_docx,
)
from db import EditorLink, OAuthConnection
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Select, Update, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import ReturningInsert

import drive_client
from auth import CurrentUserDep, SessionDep
from documents import (
    _REVISION_SOURCE_PULL,
    DocumentResponse,
    _require_content_within_cap,
    _require_owned_document,
    _revision_insert_stmt,
    _update_stmt,
    derive_content_text,
)
from drive_client import DriveAuthError, DriveError

logger = structlog.get_logger(__name__)

# Mounted on the documents resource so paths are /documents/{id}/editor-link/*.
router = APIRouter(prefix="/documents", tags=["editor-links"])

# The only provider this slice supports. 'microsoft' lands in Phase 46
# (config-only; the route shapes stay generic).
_PROVIDER_GOOGLE = "google"

# editor_links.state values (server-managed; never client-supplied).
_STATE_LINKED = "linked"
_STATE_ERROR = "error"
_STATE_UNLINKED = "unlinked"

# Postgres unique-violation SQLSTATE — the partial-unique
# ``uq_editor_links_one_linked_per_document`` backstop for a concurrent second
# link (the route's pre-check catches the common case; this catches the race).
_PG_UNIQUE_VIOLATION = "23505"


# --- request / response models ----------------------------------------------


class UnlinkRequest(BaseModel):
    """Unlink body — the mandatory user choice. ``extra="forbid"`` (Phase 18).

    ``pull-final`` runs the pull pipeline once (snapshot + overwrite) THEN
    detaches, so the latest Google edits are kept in-app. ``keep-app`` detaches
    and leaves ``content`` untouched (the in-app version wins). A smuggled field
    is a hard 422.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["pull-final", "keep-app"]


class EditorLinkResponse(BaseModel):
    """The link/pull-affecting response — state + the open URL + version cursor."""

    state: str
    web_url: str
    last_remote_version: str | None


class EditorLinkStatusResponse(BaseModel):
    """Status — adds ``remote_changed`` (the version compare) + the account email."""

    state: str
    web_url: str
    remote_changed: bool
    provider_account_email: str


# --- statement builders (tenant compile-pin seam) ----------------------------


def _connection_stmt(user_id: uuid.UUID, provider: str) -> Select[tuple[OAuthConnection]]:
    """The (JWT user, provider) OAuth connection lookup — tenant-scoped.

    Scoped by ``user_id`` (ALWAYS JWT-derived) AND ``provider``. Used to fetch
    the encrypted refresh/access tokens before a Drive call. A never-connected
    provider matches nothing -> the route's 409 "connect Google first" (NOT a
    doc-existence 404 oracle).
    """
    return select(OAuthConnection).where(
        OAuthConnection.user_id == user_id,
        OAuthConnection.provider == provider,
    )


def _linked_row_stmt(document_id: uuid.UUID, user_id: uuid.UUID) -> Select[tuple[EditorLink]]:
    """The LIVE (state='linked') editor-link row for an owned document.

    Triply-predicated: ``document_id``, ``user_id`` (ALWAYS JWT-derived — the
    denormalized tenant gate, no join back to documents), AND
    ``state='linked'``. Drop ``user_id`` and any authenticated user reads any
    user's link. Used by status / pull / unlink and the link pre-check.
    """
    return select(EditorLink).where(
        EditorLink.document_id == document_id,
        EditorLink.user_id == user_id,
        EditorLink.state == _STATE_LINKED,
    )


def _link_insert_stmt(
    *,
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    provider: str,
    provider_file_id: str,
    web_url: str,
    last_remote_version: str | None,
) -> ReturningInsert[tuple[uuid.UUID]]:
    """INSERT a new ``state='linked'`` row scoped to the JWT user.

    ``user_id`` is ALWAYS the JWT-derived value, NEVER a body/path value (the
    denormalized tenant gate). The partial-unique index enforces one live link
    per document at the DB; a race raises ``IntegrityError`` (23505) the route
    maps to 409. ``RETURNING id`` lets the route confirm the insert.
    """
    return (
        insert(EditorLink)
        .values(
            document_id=document_id,
            user_id=user_id,
            provider=provider,
            provider_file_id=provider_file_id,
            web_url=web_url,
            last_remote_version=last_remote_version,
            state=_STATE_LINKED,
        )
        .returning(EditorLink.id)
    )


def _set_version_stmt(
    *,
    link_id: uuid.UUID,
    user_id: uuid.UUID,
    last_remote_version: str | None,
) -> Update:
    """Bump ``last_remote_version`` on an owned link (after a pull). Tenant-scoped.

    Scoped by the row ``id`` AND ``user_id`` (ALWAYS JWT-derived). ``updated_at``
    is bumped EXPLICITLY via ``func.now()`` (no ``onupdate`` on the column).
    """
    return (
        update(EditorLink)
        .where(EditorLink.id == link_id, EditorLink.user_id == user_id)
        .values(last_remote_version=last_remote_version, updated_at=func.now())
    )


def _set_state_stmt(*, link_id: uuid.UUID, user_id: uuid.UUID, state: str) -> Update:
    """Set ``state`` on an owned link (error flip / unlink). Tenant-scoped.

    Scoped by the row ``id`` AND ``user_id`` (ALWAYS JWT-derived). ``updated_at``
    is bumped EXPLICITLY via ``func.now()`` (no ``onupdate`` on the column).
    """
    return (
        update(EditorLink)
        .where(EditorLink.id == link_id, EditorLink.user_id == user_id)
        .values(state=state, updated_at=func.now())
    )


# --- shared helpers ----------------------------------------------------------


def _is_unique_violation(exc: IntegrityError) -> bool:
    """True if *exc* is a Postgres unique-violation (SQLSTATE 23505).

    Driver-portable: asyncpg exposes ``sqlstate``, psycopg exposes ``pgcode``,
    on the wrapped ``.orig``. A final fallback matches the partial-unique index
    name in the message so the 409 backstop holds even if neither attribute is
    present. Used only to map the partial-unique race to a 409 (no oracle).
    """
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if code == _PG_UNIQUE_VIOLATION:
        return True
    return "uq_editor_links_one_linked_per_document" in str(exc)


def _no_linked_row() -> HTTPException:
    """The uniform 404 for an owned doc that has no LIVE editor link.

    Same byte-identical shape as the owned-document 404 (no existence oracle):
    a caller cannot distinguish "doc not yours" from "doc yours but not linked".
    """
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")


async def _require_google_connection(
    user_id: uuid.UUID,
    session: AsyncSession,
) -> OAuthConnection:
    """Return the JWT user's Google OAuth connection, or 409 "connect first".

    A never-connected user gets a 409 (a state-precondition, NOT a 404 oracle on
    the document). ``user_id`` is ALWAYS the JWT-derived value.
    """
    connection = (
        await session.execute(_connection_stmt(user_id, _PROVIDER_GOOGLE))
    ).scalar_one_or_none()
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connect your Google account before linking to Google Docs.",
        )
    return connection


async def _run_pull_overwrite(
    *,
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    link: EditorLink,
    access_token: str,
    prior_content: dict[str, object],
    prior_content_text: str,
    prior_schema_version: int,
    session: AsyncSession,
) -> DocumentResponse:
    """The pull pipeline body — snapshot-first overwrite in ONE transaction.

    Order (mirrors ``documents.import_document_docx`` steps 5-6, atomic):

    1. ``export_markdown`` the Doc (cap-check against ``MAX_CONTENT_BYTES`` via
       the converted JSON, 413 below) -> ``convert_from_markdown`` (normalizes
       ``http:///read/`` -> ``/read/``, pandoc md->html, the Node html->TipTap
       leg). A conversion failure is a fixed-detail 502.
    2. RE-DERIVE ``content_text`` (never trust the conversion) + re-cap (413).
    3. INSERT the PRIOR content/content_text/user_id into
       ``sermon_doc_revisions`` with ``source='pull'`` — BEFORE the overwrite,
       so a pull is never destructive.
    4. UPDATE ``documents.content`` + re-derived ``content_text`` + bump
       ``updated_at`` (``_update_stmt``).
    5. Bump ``editor_links.last_remote_version`` to the FRESH ``files.version``.
    6. Commit — one transaction; a partial commit can never leave a stale link.

    Returns the full ``DocumentResponse`` so the editor reloads its buffer.
    """
    fresh_version = await drive_client.get_version(access_token, link.provider_file_id)
    markdown = await drive_client.export_markdown(access_token, link.provider_file_id)
    try:
        content_json = convert_from_markdown(markdown)
    except ConversionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Document pull failed.",
        ) from exc

    _require_content_within_cap(content_json)
    new_content_text = derive_content_text(content_json)

    # 3. Snapshot-FIRST (source='pull'), holding the PRIOR state + JWT user_id.
    await session.execute(
        _revision_insert_stmt(
            document_id=document_id,
            user_id=user_id,
            content=prior_content,
            content_text=prior_content_text,
            schema_version=prior_schema_version,
            source=_REVISION_SOURCE_PULL,
        ),
    )
    # 4. Overwrite the document content (tenant-scoped, bumps updated_at).
    row = (
        await session.execute(
            _update_stmt(
                document_id,
                user_id,
                values={"content": content_json, "content_text": new_content_text},
            ),
        )
    ).one()
    # 5. Advance the version cursor so the next status compare is accurate.
    await session.execute(
        _set_version_stmt(
            link_id=link.id,
            user_id=user_id,
            last_remote_version=fresh_version,
        ),
    )
    # 6. One commit — snapshot + overwrite + cursor bump land together.
    await session.commit()
    (
        document_id_val,
        title,
        content,
        content_text,
        schema_version,
        scope_book_ids,
        scope_collection_ids,
        created_at,
        updated_at,
    ) = row
    return DocumentResponse(
        document_id=document_id_val,
        title=title,
        content=content,
        content_text=content_text,
        schema_version=schema_version,
        scope_book_ids=[uuid.UUID(book_id) for book_id in scope_book_ids],
        scope_collection_ids=[
            uuid.UUID(collection_id) for collection_id in scope_collection_ids
        ],
        created_at=created_at,
        updated_at=updated_at,
    )


# --- routes ------------------------------------------------------------------


@router.post("/{document_id}/editor-link", response_model=EditorLinkResponse)
async def link_document(
    document_id: str,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> EditorLinkResponse:
    """LINK the owned document to a NATIVE Google Doc. 404 / 409 / 502 per the plan.

    1. Owned-document gate FIRST (404-no-oracle).
    2. Pre-check: a live link already exists -> 409 (the partial-unique is the
       DB backstop, caught as 23505 -> the same 409 on a race).
    3. The JWT user's Google connection (409 "connect first" if none).
    4. Export ``content`` to ``.docx`` (``convert_to_docx``) — 502 on failure.
    5. ``get_access_token`` (cache/refresh) -> upload-with-conversion ->
       ``webViewLink`` -> ``version``.
    6. INSERT the ``state='linked'`` row; return ``{web_url, state, version}``.
    """
    document = await _require_owned_document(document_id, current_user.user_id, session)

    already = (
        await session.execute(_linked_row_stmt(document.document_id, current_user.user_id))
    ).scalar_one_or_none()
    if already is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document is already linked to an external editor.",
        )

    connection = await _require_google_connection(current_user.user_id, session)

    try:
        docx_bytes = convert_to_docx(document.content)
    except ConversionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Document export failed.",
        ) from exc

    access_token = await drive_client.get_access_token(connection, session)
    file_id = await drive_client.upload_with_conversion(
        access_token,
        name=document.title,
        docx_bytes=docx_bytes,
    )
    web_url = await drive_client.get_web_view_link(access_token, file_id)
    version = await drive_client.get_version(access_token, file_id)

    try:
        await session.execute(
            _link_insert_stmt(
                document_id=document.document_id,
                user_id=current_user.user_id,
                provider=_PROVIDER_GOOGLE,
                provider_file_id=file_id,
                web_url=web_url,
                last_remote_version=version,
            ),
        )
        await session.commit()
    except IntegrityError as exc:
        # Concurrent second link raced past the pre-check -> the partial-unique
        # fired. Same 409 as the pre-check (no oracle on the race).
        await session.rollback()
        if _is_unique_violation(exc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Document is already linked to an external editor.",
            ) from exc
        raise

    logger.info(
        "editor_link_created",
        provider=_PROVIDER_GOOGLE,
        user_id=str(current_user.user_id),
        document_id=str(document.document_id),
    )
    return EditorLinkResponse(state=_STATE_LINKED, web_url=web_url, last_remote_version=version)


@router.get("/{document_id}/editor-link/status", response_model=EditorLinkStatusResponse)
async def link_status(
    document_id: str,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> EditorLinkStatusResponse:
    """Is the linked Doc changed remotely? Compare ``version`` to the stored cursor.

    Owned-document gate -> the LIVE link row (404 if none). ``get_version`` vs
    ``last_remote_version`` -> ``remote_changed`` (equality compare, never
    parsed). A ``DriveAuthError`` (refresh token expired) flips the row to
    ``state='error'`` and returns ``state='error'`` (a re-connect signal, not a
    500); any other Drive failure is a 502.
    """
    document = await _require_owned_document(document_id, current_user.user_id, session)
    link = (
        await session.execute(_linked_row_stmt(document.document_id, current_user.user_id))
    ).scalar_one_or_none()
    if link is None:
        raise _no_linked_row()

    connection = await _require_google_connection(current_user.user_id, session)

    try:
        access_token = await drive_client.get_access_token(connection, session)
        version = await drive_client.get_version(access_token, link.provider_file_id)
    except DriveAuthError:
        await session.execute(
            _set_state_stmt(link_id=link.id, user_id=current_user.user_id, state=_STATE_ERROR),
        )
        await session.commit()
        return EditorLinkStatusResponse(
            state=_STATE_ERROR,
            web_url=link.web_url,
            remote_changed=False,
            provider_account_email=connection.provider_account_email,
        )
    except DriveError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Document link status failed.",
        ) from exc

    remote_changed = version != link.last_remote_version
    return EditorLinkStatusResponse(
        state=link.state,
        web_url=link.web_url,
        remote_changed=remote_changed,
        provider_account_email=connection.provider_account_email,
    )


@router.post("/{document_id}/editor-link/pull", response_model=DocumentResponse)
async def pull_document(
    document_id: str,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> DocumentResponse:
    """PULL the Doc's markdown back into ``content`` (snapshot-first, atomic).

    Owned-document gate -> the LIVE link row (404 if none). Then the pull
    pipeline (``_run_pull_overwrite``): snapshot the CURRENT content into
    ``sermon_doc_revisions`` with ``source='pull'`` FIRST, then export markdown
    -> normalize ``http:///read/`` -> pandoc -> the Node TipTap leg -> overwrite
    ``content`` (``content_text`` re-derived) -> bump ``last_remote_version`` —
    ALL in one transaction. Returns the full ``DocumentResponse`` so the editor
    reloads its buffer. A ``DriveAuthError`` flips the row to ``state='error'``
    and 502s the re-connect prompt; other Drive/convert failures are 502.
    """
    document = await _require_owned_document(document_id, current_user.user_id, session)
    link = (
        await session.execute(_linked_row_stmt(document.document_id, current_user.user_id))
    ).scalar_one_or_none()
    if link is None:
        raise _no_linked_row()

    connection = await _require_google_connection(current_user.user_id, session)

    try:
        access_token = await drive_client.get_access_token(connection, session)
        response = await _run_pull_overwrite(
            document_id=document.document_id,
            user_id=current_user.user_id,
            link=link,
            access_token=access_token,
            prior_content=document.content,
            prior_content_text=document.content_text,
            prior_schema_version=document.schema_version,
            session=session,
        )
    except DriveAuthError as exc:
        await session.rollback()
        await session.execute(
            _set_state_stmt(link_id=link.id, user_id=current_user.user_id, state=_STATE_ERROR),
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google access expired; reconnect required.",
        ) from exc
    except DriveError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Document pull failed.",
        ) from exc

    logger.info(
        "editor_link_pulled",
        provider=_PROVIDER_GOOGLE,
        user_id=str(current_user.user_id),
        document_id=str(document.document_id),
    )
    return response


@router.post("/{document_id}/editor-link/unlink", response_model=EditorLinkResponse)
async def unlink_document(
    document_id: str,
    payload: UnlinkRequest,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> EditorLinkResponse:
    """UNLINK with the mandatory user choice. 404 if no live link; 422 on a smuggled field.

    Owned-document gate -> the LIVE link row (404 if none). ``mode='pull-final'``
    runs the pull pipeline ONCE (snapshot + overwrite) then detaches;
    ``mode='keep-app'`` detaches and leaves ``content`` untouched. Either way the
    row is set ``state='unlinked'`` and the app-created Doc is best-effort
    deleted (failure swallowed). Returns ``{state:'unlinked', ...}``.
    """
    document = await _require_owned_document(document_id, current_user.user_id, session)
    link = (
        await session.execute(_linked_row_stmt(document.document_id, current_user.user_id))
    ).scalar_one_or_none()
    if link is None:
        raise _no_linked_row()

    connection = await _require_google_connection(current_user.user_id, session)
    web_url = link.web_url
    file_id = link.provider_file_id
    last_version = link.last_remote_version

    try:
        access_token = await drive_client.get_access_token(connection, session)
    except DriveAuthError as exc:
        # Cannot reach Drive to pull-final or delete; flip to error so the user
        # re-connects rather than silently losing the latest Doc edits.
        await session.execute(
            _set_state_stmt(link_id=link.id, user_id=current_user.user_id, state=_STATE_ERROR),
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google access expired; reconnect required.",
        ) from exc
    except DriveError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Document unlink failed.",
        ) from exc

    if payload.mode == "pull-final":
        try:
            await _run_pull_overwrite(
                document_id=document.document_id,
                user_id=current_user.user_id,
                link=link,
                access_token=access_token,
                prior_content=document.content,
                prior_content_text=document.content_text,
                prior_schema_version=document.schema_version,
                session=session,
            )
        except DriveError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Document unlink pull failed.",
            ) from exc
        # The pull bumped the cursor to the fresh Drive version; reflect it in
        # the response (the link row in memory is stale).
        last_version = await drive_client.get_version(access_token, file_id)

    # Detach: set state='unlinked' (the row survives for audit; re-link allowed
    # by the partial-unique predicate). Tenant-scoped.
    await session.execute(
        _set_state_stmt(link_id=link.id, user_id=current_user.user_id, state=_STATE_UNLINKED),
    )
    await session.commit()

    # Best-effort remove the app-created Doc (swallow failure — the local detach
    # is authoritative).
    await drive_client.delete_file(access_token, file_id)

    logger.info(
        "editor_link_unlinked",
        provider=_PROVIDER_GOOGLE,
        user_id=str(current_user.user_id),
        document_id=str(document.document_id),
        mode=payload.mode,
    )
    return EditorLinkResponse(
        state=_STATE_UNLINKED,
        web_url=web_url,
        last_remote_version=last_version,
    )
