"""Phase 43 — sermon_doc_revisions: prior-content snapshots for docx import.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-15

Adds one table — the snapshot-first half of the B2 docx round-trip (Phase
43). ``POST /documents/{document_id}/import`` accepts an attacker-controlled
.docx, converts it through pandoc + the ``worker.convert`` Node leg, and
OVERWRITES ``documents.content``. Before that overwrite, in ONE transaction,
the API inserts the CURRENT (pre-overwrite) content here so an import is
never destructive:

- ``sermon_doc_revisions(revision_id, document_id, user_id, content,
  content_text, schema_version, source, created_at)`` — one row per saved
  prior snapshot. ``content`` is the PRIOR ProseMirror/TipTap JSON node tree
  (JSONB); ``content_text`` is the prior server-derived plain-text
  projection (re-derived, never trusted from the client). ``schema_version``
  mirrors ``documents.schema_version`` (DEFAULT 1, server-managed).
  ``source`` records what triggered the snapshot (DEFAULT ``'import'``).

``user_id`` is DENORMALIZED — duplicated from the owning ``documents`` row —
so the revision tenant gate filters by the JWT-derived ``user_id`` WITHOUT a
join back to ``documents`` (which may itself be soft-deleted). It carries
its own FK -> ``users.user_id`` ON DELETE CASCADE; revisions are meaningless
once their user is gone.

Two FKs, both ON DELETE CASCADE:

- ``document_id`` -> ``documents.document_id`` — a revision is a snapshot OF
  a document; a real document row delete cascades its snapshots away (the
  documents API soft-deletes, so this fires only on a real row delete).
- ``user_id`` -> ``users.user_id`` — like every user-owned table.

The ``(document_id, created_at DESC)`` index is the revision-history hot
path: newest-snapshot-first per document. The DESC ordering is expressed
with ``sa.text("created_at DESC")`` because alembic's plain column list
cannot carry an ordering modifier (same trick as 0006's
``ix_documents_user_updated``).

Locking: brand-new table — no rewrite or scan of populated tables; the FKs
take only brief SHARE ROW EXCLUSIVE locks on ``documents`` and ``users`` for
the catalog change, safe at this deployment's size.

Hand-written (same convention as 0001–0007).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sermon_doc_revisions",
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        # DENORMALIZED owner — duplicated from the documents row so the
        # tenant gate filters here without a join back to documents (which
        # may be soft-deleted). See module docstring.
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column(
            "schema_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'import'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.document_id"],
            name="fk_sermon_doc_revisions_document_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_sermon_doc_revisions_user_id",
            ondelete="CASCADE",
        ),
    )
    # (document_id, created_at DESC) — the revision-history hot path
    # (newest-first per document). The DESC is carried by sa.text() since a
    # plain column list can't express ordering (same trick as 0006).
    op.create_index(
        "ix_sermon_doc_revisions_document_created",
        "sermon_doc_revisions",
        ["document_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sermon_doc_revisions_document_created",
        table_name="sermon_doc_revisions",
    )
    op.drop_table("sermon_doc_revisions")
