"""Phase 45 — editor_links: live link from a sermon to an external editor.

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-22

Adds one table — the storage half of the B4 Google-Docs round-trip (Phase
45). ``POST /documents/{document_id}/editor-link`` exports the canonical
``documents.content`` to a NATIVE Google Doc (upload-with-conversion via the
api-side ``drive_client``) and records one row here. While a row is
``state='linked'`` the in-app editor is HARD read-only and the user edits the
native Doc; ``.../pull`` re-imports the Doc's ``text/markdown`` export back
into ``documents.content`` (snapshot-first into ``sermon_doc_revisions`` with
``source='pull'``, so a pull is never destructive); ``.../unlink`` detaches.

- ``editor_links(id, document_id, user_id, provider, provider_file_id,
  web_url, state, last_remote_version, created_at, updated_at)`` — one row per
  external-editor link. ``provider`` is generic text ('google' now;
  'microsoft' in Phase 46). ``provider_file_id`` is the Drive file id — an
  UNTRUSTED echo: stored + returned, but the routes ONLY ever use the id from
  the user's OWN row into FIXED Google endpoints, NEVER to assemble an
  attacker-controlled URL (the SSRF guard). ``web_url`` is the Drive
  ``webViewLink`` (opened with ``rel=noopener``). ``state`` is
  ``linked``/``error``/``unlinked`` (DEFAULT ``'linked'``).
  ``last_remote_version`` is the Drive ``files.version`` cursor — COMPARED for
  equality to detect remote edits, NEVER parsed/ordered (NULLABLE).

``user_id`` is DENORMALIZED — duplicated from the owning ``documents`` row
(like ``sermon_doc_revisions``) — so the tenant gate filters links by the
JWT-derived ``user_id`` WITHOUT a join back to ``documents`` (which may be
soft-deleted). It carries its own FK -> ``users.user_id`` ON DELETE CASCADE.
``document_id`` -> ``documents.document_id`` is also ON DELETE CASCADE — a
link is meaningless once its document or user is gone.

The load-bearing constraint is the PARTIAL UNIQUE index
``uq_editor_links_one_linked_per_document`` ON ``(document_id) WHERE
state = 'linked'`` — at most ONE live external editor per document, so a
second concurrent POST link hits 23505 which the route maps to 409. It MUST
be a Postgres partial INDEX (``postgresql_where``), NOT a table
``UniqueConstraint`` — a plain unique on ``document_id`` would forbid even
``unlinked`` / ``error`` rows and break re-linking after unlink.
``ix_editor_links_user_id`` serves the per-user scan (the denormalized gate).

``updated_at`` carries ``server_default=func.now()`` for the insert but has
NO ``onupdate`` (the schema-wide convention): a state change / version bump
sets it EXPLICITLY via ``func.now()`` (the ``documents`` / ``oauth_connections``
precedent).

Locking: brand-new table — no rewrite or scan of populated tables; the two
FKs take only brief SHARE ROW EXCLUSIVE locks on ``users`` and ``documents``
for the catalog change, safe at this deployment's size (the 0008/0009 note).

Hand-written (same convention as 0001–0009).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "editor_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        # DENORMALIZED owner — duplicated from the documents row so the tenant
        # gate filters here without a join back to documents (which may be
        # soft-deleted). See module docstring.
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Generic provider key — 'google' now, 'microsoft' in Phase 46.
        sa.Column("provider", sa.Text(), nullable=False),
        # Drive file id — UNTRUSTED echo only; never used to build attacker
        # URLs (fixed Google endpoints only). The SSRF guard.
        sa.Column("provider_file_id", sa.Text(), nullable=False),
        # Drive webViewLink — opened in the browser with rel=noopener.
        sa.Column("web_url", sa.Text(), nullable=False),
        # linked | error | unlinked. Server-managed; never client-supplied.
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'linked'"),
        ),
        # Drive files.version cursor — COMPARED for equality, NEVER
        # parsed/ordered (NULLABLE until the first version fetch).
        sa.Column("last_remote_version", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # No ``onupdate`` (schema-wide convention) — a state change / version
        # bump sets this explicitly via func.now().
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.document_id"],
            name="fk_editor_links_document_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_editor_links_user_id",
            ondelete="CASCADE",
        ),
    )
    # PARTIAL UNIQUE: at most one LIVE external editor per document. MUST be a
    # partial index (postgresql_where), NOT a UniqueConstraint — a plain unique
    # on document_id would forbid unlinked/error rows and break re-linking
    # after unlink. A second concurrent link -> 23505 -> the route's 409.
    op.create_index(
        "uq_editor_links_one_linked_per_document",
        "editor_links",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("state = 'linked'"),
    )
    # Per-user scan (the denormalized tenant gate's hot path).
    op.create_index(
        "ix_editor_links_user_id",
        "editor_links",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_editor_links_user_id",
        table_name="editor_links",
    )
    op.drop_index(
        "uq_editor_links_one_linked_per_document",
        table_name="editor_links",
    )
    op.drop_table("editor_links")
