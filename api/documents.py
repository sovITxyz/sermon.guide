"""Sermon document routes — user-owned TipTap/ProseMirror JSON storage.

Phase 34 (B2 slice A): the storage + API half of the sermon editor. The
canonical sermon body is ProseMirror/TipTap JSON in ``documents.content``
(JSONB, Cross-item contract — markdown-canonical was rejected because a
citation node's structured attrs cannot round-trip through string syntax).
``content_text`` is a server-derived plain-text projection used for list
previews and future FTS; it is NEVER accepted from the client — the server
re-derives it from ``content`` on every write by walking the node tree.
Phases 35-37 build the web side on this surface; there are no web changes
here.

- ``POST /documents`` — create. Body is ``{title, content}`` (extra
  forbidden). ``schema_version`` is server-managed (the ``SCHEMA_VERSION``
  constant), never client-supplied; ``content_text`` is derived. Content
  whose serialized JSON exceeds ``MAX_CONTENT_BYTES`` is a 413.
- ``GET /documents`` — list the caller's NON-deleted docs, ``updated_at``
  DESC, each carrying a ``content_text`` PREVIEW (first
  ``PREVIEW_CHARS`` chars) — never the full ``content`` JSON.
- ``GET /documents/{document_id}`` — the full document (including
  ``content``). A non-owned, nonexistent, or soft-deleted id is a uniform
  404 with no existence oracle.
- ``PATCH /documents/{document_id}`` — partial update of ``title`` and/or
  ``content``. ``base_updated_at`` is REQUIRED and gates single-author
  optimistic concurrency: a mismatch against the stored ``updated_at`` is a
  409. On a ``content`` change ``content_text`` is re-derived; ``updated_at``
  is bumped EXPLICITLY (the column has ``server_default`` but no
  ``onupdate``).
- ``DELETE /documents/{document_id}`` — soft delete (sets ``deleted_at``);
  the row vanishes from the list and GET, but the bytes survive for restore.
- ``POST /documents/{document_id}/restore`` — clears ``deleted_at``;
  idempotent on an already-active doc; 404 if not owned.

## Tenant gate (load-bearing)

``documents`` is user-owned like ``highlights``: EVERY query filters by
``user_id`` derived from the JWT (``current_user.user_id``), never from the
body, query params, or path. The per-id endpoints resolve the row through
``_require_owned_document`` FIRST — non-UUID garbage, nonexistent ids,
another tenant's doc, AND a soft-deleted doc all collapse to one
byte-identical 404 (the ``uploads.py`` ``GET /tasks/{task_id}`` /
``reader.py`` no-existence-oracle contract). Path/body ids are never
capabilities.

Every statement is factored into a module-level ``_xxx_stmt`` builder so the
``user_id`` scoping can be compile-pinned in ``tests/test_documents_unit.py``
without a live database (the ``library._library_stmt`` pattern) — the
mechanical tenant audit.

Request models set ``extra="forbid"`` (Phase 18): a smuggled ``user_id`` or
``content_text`` is a hard 422 naming the field, never a silently-dropped
key.
"""

# python-magic is a thin untyped ctypes wrapper around libmagic (Phase 43
# docx-import sniff) — the same pyright relaxation as ``uploads.py`` /
# ``worker/extractors/extract.py``.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import json
import re
import shutil
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, cast

import magic
from convert import ConversionError, convert_from_docx, convert_to_docx
from db import Collection, Document, SermonDocRevision, UserLibraryEntry
from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import ReturningInsert, ReturningUpdate

from auth import CurrentUserDep, SessionDep
from settings import settings

router = APIRouter(prefix="/documents", tags=["documents"])

# The DOCX wire MIME — the value libmagic sniffs for a real Word .docx (an
# OOXML zip container) and the Content-Type we stream the export with. A
# single constant so the import sniff and the export header can never drift.
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# libmagic needs only the head of the stream to recognize the OOXML zip
# container/signature. 8 KiB is comfortably past the `[Content_Types].xml`
# entry pandoc writes first (the `uploads.py` _SNIFF_BYTES rationale).
_DOCX_SNIFF_BYTES = 8192

# Streaming-read chunk for the multipart import body (the `uploads.py`
# _CHUNK_BYTES value). Each chunk counts against MAX_CONTENT_BYTES so an
# oversize upload is a 413 before pandoc ever sees it.
_IMPORT_CHUNK_BYTES = 1 << 20  # 1 MiB

# Anything outside [A-Za-z0-9._-] becomes `_` when shaping the export
# download filename from the (user-controlled) document title — the same
# sanitize class as `uploads._FILENAME_SANITIZE`. A clean basename keeps the
# Content-Disposition header free of header-injection / path characters.
_FILENAME_SANITIZE = re.compile(r"[^A-Za-z0-9._-]")

# What the snapshot row records as having triggered it. The migration's
# column DEFAULT is ``'import'``; the route sets it explicitly so the value
# is authoritative regardless of the DB default. ``_REVISION_SOURCE_PULL`` tags
# the snapshot taken before a Google-Docs pull overwrite (Phase 45,
# ``editor_links.py``) so the revision history distinguishes a docx import from
# an external-editor pull.
_REVISION_SOURCE_IMPORT = "import"
_REVISION_SOURCE_PULL = "pull"

# Server-managed ProseMirror schema version stamped on every write. A
# module constant (the authoritative source per the Phase 34 pre-made
# decision); the DB column DEFAULT is only a backstop. Never client-supplied.
SCHEMA_VERSION = 1

# List-preview budget: the first N chars of the server-derived
# ``content_text``. The list never ships the full ``content`` JSON — a
# preview keeps the sermon-list response small (the GET-full endpoint
# returns the whole document).
PREVIEW_CHARS = 280

# Hard cap on the serialized ``content`` JSON, measured in bytes (the Phase
# 34 pre-made decision: measured on the serialized JSON byte size, enforced
# in-handler -> 413). ~2 MB — a single sermon is kilobytes; this only stops
# a pathological or abusive payload, mirroring the ``/upload`` size cap.
MAX_CONTENT_BYTES = 2 * 1024 * 1024

# Per-sermon citation-scope caps (Phase 50). The API is the single 422 owner
# (the web whitelists do structural-only checks); these mirror the Phase 49
# scoped-search caps so the editor's Scope control and the search path agree on
# the limits. A whole 10K-book library can be scoped ad-hoc; collections are far
# fewer.
SCOPE_BOOK_IDS_CAP = 10_000
SCOPE_COLLECTION_IDS_CAP = 500


class DocumentCreate(BaseModel):
    """POST body. No ``user_id``/``content_text``/``schema_version`` fields.

    ``extra="forbid"`` (Phase 18 posture): a smuggled ``user_id`` is a hard
    422, never a silently-dropped key — the tenant invariant made
    mechanical. ``content_text`` is likewise forbidden: the server
    re-derives it from ``content`` on every write, so a client-supplied
    value (which could disagree with ``content``) must fail loud.
    ``content`` is the ProseMirror/TipTap JSON node tree (an arbitrary JSON
    object); its byte size is capped in-handler.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=512)
    content: dict[str, object]
    # Per-sermon citation scope (Phase 50). Default empty = whole library. The
    # API clamps each set to the JWT user's library / owned collections on
    # write, so a smuggled foreign id is silently dropped (not an oracle-leaking
    # error). Caps are the single 422 owner.
    scope_book_ids: list[uuid.UUID] = Field(default_factory=list, max_length=SCOPE_BOOK_IDS_CAP)
    scope_collection_ids: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=SCOPE_COLLECTION_IDS_CAP,
    )


class DocumentUpdate(BaseModel):
    """PATCH body — partial (title/content/scope); base_updated_at REQUIRED.

    ``extra="forbid"`` (Phase 18): a smuggled ``user_id`` / ``content_text``
    is a hard 422. ``base_updated_at`` is the optimistic-concurrency token:
    it MUST equal the stored ``updated_at`` or the PATCH is a 409 (single
    author, no versions table — B2). ``title``, ``content``, and the two scope
    arrays are all optional, but at least one must be present (an empty patch is
    a 422). The scope arrays are three-state: ABSENT (``None`` — leave the
    stored value) vs present (replace it, where ``[]`` clears the scope to whole
    library). Each present scope set is clamped to the JWT user's library /
    owned collections on write.
    """

    model_config = ConfigDict(extra="forbid")

    base_updated_at: datetime
    title: str | None = Field(default=None, min_length=1, max_length=512)
    content: dict[str, object] | None = None
    scope_book_ids: list[uuid.UUID] | None = Field(default=None, max_length=SCOPE_BOOK_IDS_CAP)
    scope_collection_ids: list[uuid.UUID] | None = Field(
        default=None,
        max_length=SCOPE_COLLECTION_IDS_CAP,
    )


class DocumentSummary(BaseModel):
    """List item — metadata + a ``content_text`` PREVIEW, never full content."""

    document_id: uuid.UUID
    title: str
    preview: str
    schema_version: int
    created_at: datetime
    updated_at: datetime


class DocumentListResponse(BaseModel):
    documents: list[DocumentSummary]


class DocumentResponse(BaseModel):
    """Full document — includes the ``content`` JSON node tree + scope."""

    document_id: uuid.UUID
    title: str
    content: dict[str, object]
    content_text: str
    schema_version: int
    # Per-sermon citation scope (Phase 50): the clamped book / collection ids the
    # sermon's citation drawer is limited to. Stored as JSONB UUID strings;
    # coerced to ``uuid.UUID`` on the way out.
    scope_book_ids: list[uuid.UUID]
    scope_collection_ids: list[uuid.UUID]
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class _Frame:
    """One container node mid-walk in :func:`derive_content_text`.

    ``children`` is the node's child list, ``parts`` accumulates the
    NON-EMPTY projections of children already resolved (in document order),
    and ``cursor`` is the index of the next child to process. A plain
    mutable struct so the iterative walk can suspend/resume a parent across
    a child descent without recursion.
    """

    children: list[object]
    parts: list[str]
    cursor: int


def _node_children(content: object) -> list[object] | None:
    """Return the child-node list for a container, or ``None`` for a leaf/scalar.

    A non-text ``dict`` whose ``content`` is a list is a block-level
    container; a bare ``list`` is treated as a container of its elements
    (the top-level ``doc.content`` handed in raw). Everything else — a text
    node, a leaf node with no ``content`` list, or a scalar — has no
    children. Text nodes are deliberately excluded here: their projection is
    their ``text`` string (handled by ``_node_text``), never a join of
    children, even if a malformed text node also carried a ``content`` list.
    """
    if isinstance(content, dict):
        node = cast("dict[str, object]", content)
        if node.get("type") == "text":
            return None
        children = node.get("content")
        if isinstance(children, list):
            return cast("list[object]", children)
        return None
    if isinstance(content, list):
        return cast("list[object]", content)
    return None


def _node_text(content: object) -> str:
    """Return a single node's own text contribution (no recursion into children).

    A ``text`` node yields its ``text`` string (``""`` if absent/non-str);
    every other node — a container (whose text comes from its children), a
    non-text leaf, or a scalar — contributes nothing on its own.
    """
    if isinstance(content, dict):
        node = cast("dict[str, object]", content)
        if node.get("type") == "text":
            text = node.get("text")
            return text if isinstance(text, str) else ""
    return ""


def derive_content_text(content: object) -> str:
    """Walk a ProseMirror/TipTap JSON node tree → plain text.

    Pure helper (no I/O), unit-tested directly. Concatenates every
    ``text``-node's ``text`` string; block-level nodes are joined with a
    newline so paragraphs/headings/list-items read as separate lines in the
    derived projection. Marks (bold/italic/links) wrap a text node's
    ``text`` and so are picked up transparently; non-text leaf nodes (an
    image, a hard break, a citation node with no text) contribute nothing.
    Malformed / non-dict input degrades to an empty string rather than
    raising — the byte-size cap and JSON-shape are the client's contract,
    this projection is best-effort.

    The walk is ITERATIVE (an explicit work stack, depth-first,
    document-order) — NOT recursive — so a pathologically deep document
    cannot raise ``RecursionError`` and 500 the request (a small but deeply
    nested payload sits well under ``MAX_CONTENT_BYTES`` yet would blow
    Python's ~1000-frame default limit). Output is byte-identical to the
    earlier recursive form: a container's projection is the newline-join of
    its children's NON-EMPTY projections (so an empty child contributes no
    blank line), evaluated in document order; a text node yields its text;
    every other leaf yields ``""``.

    Mechanics: post-order DFS over an explicit ``stack`` of ``_Frame``
    items, each ``(children, parts, cursor)`` for one container node.
    ``cursor`` walks the frame's children left-to-right: a leaf child folds
    its own text straight into the frame's ``parts``; a container child
    pushes a new frame and suspends the parent. When a frame's children are
    exhausted, its non-empty ``parts`` are newline-joined and that string is
    appended to the parent frame's ``parts`` (the top frame's join is the
    return value). No call recursion, so depth is bounded only by available
    memory, not the interpreter stack.

    The output backs list previews (first ``PREVIEW_CHARS`` chars) and
    future FTS. It is re-derived on EVERY write; the client never supplies
    it.
    """
    children = _node_children(content)
    if children is None:
        # A leaf (text node or otherwise) or scalar: its own text, no walk.
        return _node_text(content)

    # Post-order DFS with an explicit stack. Each frame is one container
    # node; ``parts`` accumulates its children's resolved (already
    # newline-folded) projections in document order. ``cursor`` is the
    # index of the next child to process. We process a frame's children
    # one at a time: a leaf child folds in immediately; a container child
    # pushes a new frame and suspends the parent (we re-find it on return).
    stack: list[_Frame] = [_Frame(children=children, parts=[], cursor=0)]
    while True:
        frame = stack[-1]
        if frame.cursor < len(frame.children):
            child = frame.children[frame.cursor]
            frame.cursor += 1
            grandkids = _node_children(child)
            if grandkids is None:
                # Leaf child: its own text folds straight into the parent.
                part = _node_text(child)
                if part:
                    frame.parts.append(part)
            else:
                # Container child: descend; its folded result is appended
                # to THIS frame's parts when the child frame completes.
                stack.append(_Frame(children=grandkids, parts=[], cursor=0))
            continue
        # All children of this frame are resolved: fold them.
        folded = "\n".join(frame.parts)
        stack.pop()
        if not stack:
            return folded
        if folded:
            stack[-1].parts.append(folded)


def _content_byte_size(content: dict[str, object]) -> int:
    """Serialized JSON byte size of *content* (the 413 measure).

    The cap is on the canonical serialized form, not the in-memory dict —
    ``ensure_ascii=False`` so multibyte text counts its real UTF-8 length,
    not an escaped expansion.
    """
    return len(json.dumps(content, ensure_ascii=False).encode("utf-8"))


def _require_content_within_cap(content: dict[str, object]) -> None:
    """413 unless *content* serializes within ``MAX_CONTENT_BYTES``."""
    size = _content_byte_size(content)
    if size > MAX_CONTENT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Document content exceeds {MAX_CONTENT_BYTES} bytes.",
        )


def _list_stmt(user_id: uuid.UUID) -> Select[tuple[uuid.UUID, str, str, int, datetime, datetime]]:
    """Build the tenant-scoped sermon list: the user's NON-deleted docs.

    Factored out so BOTH the ``user_id`` filter and the
    ``deleted_at IS NULL`` predicate can be compile-pinned without a live
    database (the ``library._library_stmt`` pattern). The ``user_id`` filter
    is the load-bearing line: drop it and every user sees every user's
    sermons. ``user_id`` is ALWAYS the JWT-derived value. ``ORDER BY
    updated_at DESC`` rides ``ix_documents_user_updated``.
    """
    return (
        select(
            Document.document_id,
            Document.title,
            Document.content_text,
            Document.schema_version,
            Document.created_at,
            Document.updated_at,
        )
        .where(
            Document.user_id == user_id,
            Document.deleted_at.is_(None),
        )
        .order_by(Document.updated_at.desc())
    )


def _owned_active_stmt(document_id: uuid.UUID, user_id: uuid.UUID) -> Select[tuple[Document]]:
    """Build the owned + ACTIVE document lookup (GET-full / PATCH / DELETE).

    Triply-predicated: ``document_id`` from the path, ``user_id`` ALWAYS
    from the JWT, and ``deleted_at IS NULL`` so a soft-deleted doc reads as
    404 (per the Verify matrix: GET on a soft-deleted doc -> 404). Drop
    ``user_id`` and any authenticated user reads any sermon — this is the
    tenant gate.
    """
    return select(Document).where(
        Document.document_id == document_id,
        Document.user_id == user_id,
        Document.deleted_at.is_(None),
    )


def _owned_any_stmt(document_id: uuid.UUID, user_id: uuid.UUID) -> Select[tuple[Document]]:
    """Build the owned lookup INCLUDING soft-deleted (restore path only).

    Restore must find a soft-deleted row, so this variant omits the
    ``deleted_at IS NULL`` predicate — but KEEPS the ``user_id`` gate so a
    cross-tenant restore is the same 404. ``user_id`` is ALWAYS the
    JWT-derived value.
    """
    return select(Document).where(
        Document.document_id == document_id,
        Document.user_id == user_id,
    )


def _owned_book_ids_stmt(
    book_ids: Sequence[uuid.UUID],
    user_id: uuid.UUID,
) -> Select[tuple[uuid.UUID]]:
    """Build the scope-clamp: which of *book_ids* the JWT user owns (library).

    ``scope_book_ids`` arrives as ATTACKER-CONTROLLED body input, so the
    requested set is INTERSECTED with the owner's ``user_library`` before it is
    persisted — a sermon's scope can never name a book the user does not own
    (the CLAUDE.md library-intersection invariant, the same guard every search
    uses). The ``user_id`` filter is load-bearing — ALWAYS the JWT value. An
    empty ``book_ids`` yields a false predicate (no rows); callers short-circuit
    before reaching here. Factored into a module-level builder so the
    ``user_id`` scoping is compile-pinned in ``tests/test_documents_unit.py``.
    """
    return select(UserLibraryEntry.book_id).where(
        UserLibraryEntry.book_id.in_(book_ids),
        UserLibraryEntry.user_id == user_id,
    )


def _owned_collection_ids_stmt(
    collection_ids: Sequence[uuid.UUID],
    user_id: uuid.UUID,
) -> Select[tuple[uuid.UUID]]:
    """Build the scope-clamp: which of *collection_ids* the JWT user owns.

    ``scope_collection_ids`` is likewise attacker-controlled body input: the
    requested set is INTERSECTED with the user's owned ``collections`` before it
    is persisted, so a foreign/nonexistent collection is silently clamped out
    (the no-oracle posture, in set form). The ``user_id`` filter is load-bearing
    — ALWAYS the JWT value. An empty ``collection_ids`` yields no rows; callers
    short-circuit first.
    """
    return select(Collection.collection_id).where(
        Collection.collection_id.in_(collection_ids),
        Collection.user_id == user_id,
    )


async def _clamp_scope_book_ids(
    book_ids: Sequence[uuid.UUID],
    user_id: uuid.UUID,
    session: AsyncSession,
) -> list[str]:
    """Intersect requested scope *book_ids* with the JWT user's library.

    Returns the owned subset as UUID strings (the JSONB column stores text), in
    the request's order with duplicates dropped. A foreign/unowned id is
    silently clamped out. An empty/absent request short-circuits with no query.
    """
    deduped = list(dict.fromkeys(book_ids))
    if not deduped:
        return []
    result = await session.execute(_owned_book_ids_stmt(deduped, user_id))
    owned = set(result.scalars().all())
    return [str(book_id) for book_id in deduped if book_id in owned]


async def _clamp_scope_collection_ids(
    collection_ids: Sequence[uuid.UUID],
    user_id: uuid.UUID,
    session: AsyncSession,
) -> list[str]:
    """Intersect requested scope *collection_ids* with the JWT user's collections.

    Returns the owned subset as UUID strings, in request order with duplicates
    dropped. A foreign/nonexistent collection is silently clamped out. An
    empty/absent request short-circuits with no query.
    """
    deduped = list(dict.fromkeys(collection_ids))
    if not deduped:
        return []
    result = await session.execute(_owned_collection_ids_stmt(deduped, user_id))
    owned = set(result.scalars().all())
    return [str(collection_id) for collection_id in deduped if collection_id in owned]


def _delete_stmt(
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    now: datetime,
) -> ReturningUpdate[tuple[uuid.UUID]]:
    """Build the soft-delete UPDATE: set ``deleted_at`` on an ACTIVE owned row.

    Scoped by ``document_id`` AND ``user_id`` (the tenant gate) AND
    ``deleted_at IS NULL`` so a second DELETE on an already-deleted doc
    matches nothing and the handler 404s — idempotency is not the contract
    here (restore is). ``RETURNING document_id`` lets the handler tell "soft
    deleted one row" from "matched nothing -> 404" without a prior SELECT.
    ``user_id`` is ALWAYS the JWT-derived value.
    """
    return (
        update(Document)
        .where(
            Document.document_id == document_id,
            Document.user_id == user_id,
            Document.deleted_at.is_(None),
        )
        .values(deleted_at=now)
        .returning(Document.document_id)
    )


def _update_stmt(
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    values: dict[str, object],
) -> ReturningUpdate[
    tuple[uuid.UUID, str, dict[str, object], str, int, list[str], list[str], datetime, datetime]
]:
    """Build the PATCH UPDATE: apply *values* + bump ``updated_at`` on an owned row.

    Scoped by ``document_id`` AND ``user_id`` (the tenant gate) AND
    ``deleted_at IS NULL`` (a soft-deleted doc is not patchable — same 404 as
    nonexistent). ``updated_at`` is bumped EXPLICITLY via ``func.now()`` in
    the value set (the column has ``server_default`` but no ``onupdate``, the
    schema-wide convention) so the new value reads back for the NEXT PATCH's
    optimistic-concurrency gate. ``RETURNING`` the full row avoids a second
    round-trip. ``user_id`` is ALWAYS the JWT-derived value.

    The 409 base-mismatch check is the caller's job (it needs the prior
    ``updated_at`` from the gate SELECT); this builder only carries the
    tenant + active predicates.
    """
    return (
        update(Document)
        .where(
            Document.document_id == document_id,
            Document.user_id == user_id,
            Document.deleted_at.is_(None),
        )
        .values(**values, updated_at=func.now())
        .returning(
            Document.document_id,
            Document.title,
            Document.content,
            Document.content_text,
            Document.schema_version,
            Document.scope_book_ids,
            Document.scope_collection_ids,
            Document.created_at,
            Document.updated_at,
        )
    )


async def _require_owned_document(
    document_id: str,
    user_id: uuid.UUID,
    session: AsyncSession,
) -> Document:
    """Return the owned, ACTIVE document or raise a no-oracle 404.

    Non-UUID garbage, nonexistent ids, another tenant's doc, AND a
    soft-deleted doc are byte-identical 404s — no existence oracle (the
    ``reader._require_owned_book`` contract). Used by GET-full / PATCH /
    DELETE; restore uses ``_owned_any_stmt`` directly because it must see
    soft-deleted rows.
    """
    not_found = HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Document not found.",
    )
    try:
        document_uuid = uuid.UUID(document_id)
    except ValueError as exc:
        # Not a UUID → cannot be a documents PK → same 404 shape.
        raise not_found from exc
    result = await session.execute(_owned_active_stmt(document_uuid, user_id))
    document = result.scalar_one_or_none()
    if document is None:
        raise not_found
    return document


def _to_response(document: Document) -> DocumentResponse:
    return DocumentResponse(
        document_id=document.document_id,
        title=document.title,
        content=document.content,
        content_text=document.content_text,
        schema_version=document.schema_version,
        scope_book_ids=[uuid.UUID(book_id) for book_id in document.scope_book_ids],
        scope_collection_ids=[
            uuid.UUID(collection_id) for collection_id in document.scope_collection_ids
        ],
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def create_document(
    payload: DocumentCreate,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> DocumentResponse:
    """Create a sermon document for the JWT user. 413 if content too large.

    The optional citation-scope arrays are CLAMPED to the JWT user's library /
    owned collections before persisting — a foreign id is silently dropped (the
    tenant invariant).
    """
    _require_content_within_cap(payload.content)
    scope_book_ids = await _clamp_scope_book_ids(
        payload.scope_book_ids,
        current_user.user_id,
        session,
    )
    scope_collection_ids = await _clamp_scope_collection_ids(
        payload.scope_collection_ids,
        current_user.user_id,
        session,
    )
    document = Document(
        user_id=current_user.user_id,
        title=payload.title,
        content=payload.content,
        content_text=derive_content_text(payload.content),
        schema_version=SCHEMA_VERSION,
        scope_book_ids=scope_book_ids,
        scope_collection_ids=scope_collection_ids,
    )
    session.add(document)
    await session.commit()
    await session.refresh(document)
    return _to_response(document)


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    current_user: CurrentUserDep,
    session: SessionDep,
) -> DocumentListResponse:
    """List the caller's non-deleted sermons, newest first, with previews."""
    result = await session.execute(_list_stmt(current_user.user_id))
    documents = [
        DocumentSummary(
            document_id=document_id,
            title=title,
            preview=content_text[:PREVIEW_CHARS],
            schema_version=schema_version,
            created_at=created_at,
            updated_at=updated_at,
        )
        for (
            document_id,
            title,
            content_text,
            schema_version,
            created_at,
            updated_at,
        ) in result.tuples().all()
    ]
    return DocumentListResponse(documents=documents)


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: str,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> DocumentResponse:
    """Return the full document for the JWT user; 404 (no oracle) otherwise."""
    document = await _require_owned_document(document_id, current_user.user_id, session)
    return _to_response(document)


@router.patch("/{document_id}", response_model=DocumentResponse)
async def update_document(
    document_id: str,
    payload: DocumentUpdate,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> DocumentResponse:
    """Partial update under optimistic concurrency. 409 on stale base, 404 no-oracle.

    ``base_updated_at`` must equal the stored ``updated_at`` (single-author
    409 gate). At least one of ``title`` / ``content`` / ``scope_book_ids`` /
    ``scope_collection_ids`` must be present. On a ``content`` change,
    ``content_text`` is re-derived and the size cap is enforced; each present
    scope set is clamped to the JWT user's library / owned collections;
    ``updated_at`` is bumped EXPLICITLY (no ``onupdate`` on the column) so the
    new value reads back for the next PATCH's gate.
    """
    if (
        payload.title is None
        and payload.content is None
        and payload.scope_book_ids is None
        and payload.scope_collection_ids is None
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="PATCH must set at least one of title, content, scope_book_ids, "
            "scope_collection_ids.",
        )
    if payload.content is not None:
        _require_content_within_cap(payload.content)

    # Gate FIRST (ownership + active + 404-no-oracle); the loaded row also
    # carries the stored updated_at for the 409 check.
    document = await _require_owned_document(document_id, current_user.user_id, session)

    # Optimistic concurrency: the client's base must match what we stored.
    # Mismatch -> 409 (another write landed since the client last read).
    if document.updated_at != payload.base_updated_at:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document was modified since base_updated_at; reload and retry.",
        )

    values: dict[str, object] = {}
    if payload.title is not None:
        values["title"] = payload.title
    if payload.content is not None:
        values["content"] = payload.content
        # content_text is server-derived on every content write — the client
        # never supplies it (extra="forbid" forbids the field outright).
        values["content_text"] = derive_content_text(payload.content)
    # Scope arrays are three-state: ``None`` = absent (leave stored), present
    # (incl. ``[]``) = replace. Each present set is clamped to the JWT user's
    # library / owned collections so a smuggled foreign id is dropped.
    if payload.scope_book_ids is not None:
        values["scope_book_ids"] = await _clamp_scope_book_ids(
            payload.scope_book_ids,
            current_user.user_id,
            session,
        )
    if payload.scope_collection_ids is not None:
        values["scope_collection_ids"] = await _clamp_scope_collection_ids(
            payload.scope_collection_ids,
            current_user.user_id,
            session,
        )

    # Statement-level UPDATE bumps updated_at via func.now() in the value set
    # (the column has server_default but no onupdate — the schema-wide
    # convention; reader._position_upsert_stmt does the same) and RETURNs the
    # fresh row.
    row = (
        await session.execute(
            _update_stmt(document.document_id, current_user.user_id, values=values),
        )
    ).one()
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


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: str,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> None:
    """Soft-delete the document (sets ``deleted_at``). 404 (no oracle) otherwise.

    A second DELETE on an already soft-deleted doc is a 404 (the active-row
    predicate matches nothing) — symmetric with GET-on-deleted; restore is
    the idempotent inverse.
    """
    not_found = HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Document not found.",
    )
    try:
        document_uuid = uuid.UUID(document_id)
    except ValueError as exc:
        raise not_found from exc
    result = await session.execute(
        _delete_stmt(document_uuid, current_user.user_id, now=datetime.now(tz=UTC)),
    )
    if result.scalar_one_or_none() is None:
        raise not_found
    await session.commit()


@router.post("/{document_id}/restore", response_model=DocumentResponse)
async def restore_document(
    document_id: str,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> DocumentResponse:
    """Clear ``deleted_at``. Idempotent on an active doc; 404 (no oracle) otherwise.

    Restore is the only endpoint that must SEE soft-deleted rows, so it
    resolves through ``_owned_any_stmt`` (no ``deleted_at IS NULL``) — but
    KEEPS the ``user_id`` gate, so a cross-tenant restore is the same 404 as
    a nonexistent id. Restoring an already-active doc is a no-op 200 (the
    pre-made idempotency decision).
    """
    not_found = HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Document not found.",
    )
    try:
        document_uuid = uuid.UUID(document_id)
    except ValueError as exc:
        raise not_found from exc
    result = await session.execute(_owned_any_stmt(document_uuid, current_user.user_id))
    document = result.scalar_one_or_none()
    if document is None:
        raise not_found
    if document.deleted_at is not None:
        document.deleted_at = None
        await session.commit()
        await session.refresh(document)
    return _to_response(document)


# === DOCX round-trip (Phase 43) ==============================================
#
# ``GET /documents/{document_id}/export.docx`` and ``POST
# /documents/{document_id}/import`` add a Word round-trip on top of the
# canonical TipTap/ProseMirror JSON. Both mount under the existing
# ``documents`` resource (the api has no ``/sermons`` prefix; the product term
# "sermons" == this resource — the web ``/sermons`` editor proxies to
# ``/api/documents/...``). Both gate through ``_require_owned_document`` FIRST,
# so a non-owned / nonexistent / non-UUID / soft-deleted id is the same
# byte-identical 404 with no existence oracle.
#
# Import is an ATTACKER-CONTROLLED .docx upload, so it reuses the ``uploads.py``
# edge defenses: libmagic-sniff the bytes (415 on non-docx) and size-cap the
# body (413) BEFORE pandoc runs, stage the bytes in ``settings.upload_dir`` with
# a ``finally`` that ALWAYS deletes the staged file, and pandoc runs with no
# network. The converted JSON is re-capped and ``content_text`` is RE-DERIVED
# (never trusted from the conversion / the client). The overwrite is
# snapshot-first: in ONE transaction the CURRENT (pre-overwrite) content is
# inserted into ``sermon_doc_revisions`` BEFORE the UPDATE, so an import is
# never destructive. The snapshot row's ``user_id`` is the JWT user — never a
# body/path value (the denormalized tenant gate, ``worker/db`` migration 0008).


def _export_filename(title: str) -> str:
    """Shape a safe ``.docx`` download filename from the document *title*.

    The title is user-controlled, so it can't go verbatim into the
    ``Content-Disposition`` header (header-injection / path characters). The
    same sanitize class as ``uploads._sanitize_filename`` collapses anything
    outside ``[A-Za-z0-9._-]`` to ``_``; an empty/all-stripped title falls
    back to ``sermon`` so the download is never named ``.docx`` alone.
    """
    cleaned = _FILENAME_SANITIZE.sub("_", title).strip("_")
    base = cleaned or "sermon"
    return f"{base}.docx"


def _revision_insert_stmt(
    *,
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    content: dict[str, object],
    content_text: str,
    schema_version: int,
    source: str = _REVISION_SOURCE_IMPORT,
) -> ReturningInsert[tuple[uuid.UUID]]:
    """Build the snapshot INSERT — the prior content row, scoped to the JWT user.

    Factored out so the tenant column (``user_id`` is ALWAYS the JWT-derived
    value, NEVER a body/path value) and the snapshot's content/content_text
    can be compile-pinned in ``tests/test_documents_unit.py`` (the
    ``_xxx_stmt`` seam). ``source`` defaults to the import sentinel but is
    parameterized so the Phase 45 Google-Docs pull (``editor_links.py``) can
    tag its snapshot ``'pull'`` distinctly. ``RETURNING revision_id`` lets the
    route assert exactly one snapshot landed.
    """
    return (
        insert(SermonDocRevision)
        .values(
            document_id=document_id,
            user_id=user_id,
            content=content,
            content_text=content_text,
            schema_version=schema_version,
            source=source,
        )
        .returning(SermonDocRevision.revision_id)
    )


def _sniff_docx(head: bytes) -> None:
    """415 unless *head* sniffs as a Word ``.docx`` (OOXML zip container).

    The sniff is over CONTENT bytes, never the client's ``Content-Type``
    header (the ``uploads.py`` edge-sniff posture) — there is no header for an
    attacker to vary. Runs BEFORE any disk write or pandoc invocation: a
    renamed file dies here instead of being staged or handed to pandoc.
    """
    mime = magic.from_buffer(head[:_DOCX_SNIFF_BYTES], mime=True)
    if mime != _DOCX_MIME:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported upload content (sniffed {mime!r}); expected a .docx.",
        )


def _read_capped_upload(file: UploadFile, *, max_bytes: int) -> bytes:
    """Read the whole multipart body into memory, 413 past *max_bytes*.

    The body streams in 1 MiB chunks (Starlette spools to a
    ``SpooledTemporaryFile``), and the running total is checked per chunk so
    an oversize upload is a 413 the moment it crosses the cap — never fully
    buffered, never staged, never handed to pandoc. ``max_bytes`` is the same
    ``MAX_CONTENT_BYTES`` ceiling the converted JSON is re-checked against.
    """
    buf = bytearray()
    while True:
        chunk = file.file.read(_IMPORT_CHUNK_BYTES)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Upload exceeds {max_bytes} bytes.",
            )
    return bytes(buf)


@router.get("/{document_id}/export.docx")
async def export_document_docx(
    document_id: str,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> Response:
    """Export the owned document as a Word ``.docx``. 404 (no oracle) otherwise.

    Gates through ``_require_owned_document`` FIRST (ownership + active +
    404-no-oracle), then converts the canonical ``content`` JSON to ``.docx``
    bytes via ``worker.convert.convert_to_docx`` (pandoc + the Node leg) and
    streams them with the docx ``Content-Type`` and a sanitized
    ``Content-Disposition`` filename derived from the title. A conversion
    failure is a 502 (a dependency — pandoc/Node — failed; not a request bug)
    with a fixed detail, never the raw conversion stack trace.
    """
    document = await _require_owned_document(document_id, current_user.user_id, session)
    try:
        docx_bytes = convert_to_docx(document.content)
    except ConversionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Document export failed.",
        ) from exc
    filename = _export_filename(document.title)
    return Response(
        content=docx_bytes,
        media_type=_DOCX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{document_id}/import", response_model=DocumentResponse)
async def import_document_docx(
    document_id: str,
    current_user: CurrentUserDep,
    session: SessionDep,
    file: Annotated[UploadFile, File(...)],
) -> DocumentResponse:
    """Replace the owned document's content from an uploaded ``.docx`` (snapshot-first).

    The full attacker-controlled-upload pipeline:

    1. **415** if the bytes don't sniff as a ``.docx`` (content sniff, not the
       header), **413** if the body exceeds ``MAX_CONTENT_BYTES`` — both BEFORE
       any disk write or pandoc run.
    2. Stage the bytes under ``settings.upload_dir`` (per-import UUID subdir);
       a ``finally`` ALWAYS deletes them, success or failure.
    3. ``_require_owned_document`` (ownership + active + 404-no-oracle) — only
       AFTER the cheap edge checks, so a non-owner's oversize/non-docx upload
       still gets the cheap 4xx without a DB hit, and an owned-doc miss is the
       same 404.
    4. ``convert_from_docx`` (pandoc docx->html, then the Node leg
       html->ProseMirror JSON) — a conversion failure is a fixed-detail 502.
    5. Re-cap the converted JSON to ``MAX_CONTENT_BYTES`` (413) and RE-DERIVE
       ``content_text`` (never trust the conversion output as the projection).
    6. **Snapshot-first** in ONE transaction: INSERT the CURRENT
       (pre-overwrite) content/content_text/user_id into
       ``sermon_doc_revisions``, THEN UPDATE ``documents.content`` /
       ``content_text`` + bump ``updated_at``, THEN commit. The snapshot
       predates the overwrite, so an import is never destructive.

    The snapshot row's ``user_id`` is the JWT user (the denormalized tenant
    gate); nothing here reads a ``user_id``/``document_id`` from the body.
    """
    # 1. Edge checks over the body bytes, BEFORE disk or pandoc.
    raw = _read_capped_upload(file, max_bytes=MAX_CONTENT_BYTES)
    _sniff_docx(raw)

    # 2. Stage under the upload dir so pandoc reads a real file path; the
    #    finally ALWAYS removes it. (worker.convert also stages in /tmp, but
    #    we keep the api-side staged copy under settings.upload_dir per the
    #    uploads.py per-upload-subdir convention so a partial write is bounded
    #    and self-cleaning.)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    import_subdir = settings.upload_dir / str(uuid.uuid4())
    import_subdir.mkdir(parents=True, exist_ok=False)
    staged = import_subdir / "import.docx"
    try:
        staged.write_bytes(raw)

        # 3. Ownership gate (after the cheap edge checks). The loaded row
        #    carries the CURRENT content/content_text for the snapshot.
        document = await _require_owned_document(document_id, current_user.user_id, session)

        # 4. Convert. A pandoc/Node failure is a fixed-detail 502, never a
        #    raw stack-trace oracle.
        try:
            content_json = convert_from_docx(staged.read_bytes())
        except ConversionError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Document import failed.",
            ) from exc

        # 5. Re-cap + RE-DERIVE content_text (never trust the conversion).
        _require_content_within_cap(content_json)
        new_content_text = derive_content_text(content_json)

        # 6. Snapshot-FIRST, then overwrite — one transaction. The snapshot
        #    holds the PRIOR content/content_text and the JWT user_id.
        prior_content = document.content
        prior_content_text = document.content_text
        prior_schema_version = document.schema_version
        await session.execute(
            _revision_insert_stmt(
                document_id=document.document_id,
                user_id=current_user.user_id,
                content=prior_content,
                content_text=prior_content_text,
                schema_version=prior_schema_version,
            ),
        )
        row = (
            await session.execute(
                _update_stmt(
                    document.document_id,
                    current_user.user_id,
                    values={"content": content_json, "content_text": new_content_text},
                ),
            )
        ).one()
        await session.commit()
    finally:
        # ALWAYS clean the staged upload — success, 502, 413, or any other
        # error. rmtree(ignore_errors=True) removes the whole per-import
        # subdir unconditionally and never raises, so cleanup can't mask the
        # real exception nor leave attacker bytes on disk.
        shutil.rmtree(import_subdir, ignore_errors=True)

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
