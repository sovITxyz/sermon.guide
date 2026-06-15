"""Phase 34 — documents: user-owned sermon storage (TipTap/ProseMirror JSON).

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-15

Adds one table — the storage half of the B2 sermon editor (slice A):

- ``documents(document_id, user_id, title, content, content_text,
  schema_version, deleted_at, created_at, updated_at)`` — one row per
  user's sermon. ``content`` is the canonical ProseMirror/TipTap JSON node
  tree (JSONB); ``content_text`` is the server-derived plain-text
  projection backing list previews / future FTS (never client-supplied,
  re-derived on every write). ``schema_version`` is server-managed
  (DEFAULT 1). ``deleted_at`` NULL = active, a timestamp = soft-deleted;
  ``POST /documents/{document_id}/restore`` clears it.

User-owned like ``highlights``: every API query filters by ``user_id``
(JWT-derived); a non-owned ``document_id`` is a uniform 404 with no
existence oracle. The FK cascades — documents are meaningless once their
user is gone.

The ``(user_id, updated_at DESC)`` index is the sermon-list hot path: it
matches the list query's ``ORDER BY updated_at DESC`` so the planner walks
it in order, with the ``user_id`` prefix scoping per tenant. The DESC
ordering is expressed with ``sa.text("updated_at DESC")`` because alembic's
plain column list cannot carry an ordering modifier; this is the first
descending index in the schema.

``updated_at`` carries ``server_default=func.now()`` for the insert but NO
``onupdate`` (the schema-wide convention): the API bumps it EXPLICITLY per
PATCH so the value is read back for the single-author optimistic-concurrency
``base_updated_at`` 409 gate.

Locking: brand-new table — no rewrite or scan of populated tables; the FK
takes only a brief SHARE ROW EXCLUSIVE lock on ``users`` for the catalog
change, safe at this deployment's size.

Hand-written (same convention as 0001–0005). First use of JSONB in the
schema — ``postgresql.JSONB()`` for the ``content`` column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("document_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column(
            "schema_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_documents_user_id",
            ondelete="CASCADE",
        ),
    )
    # (user_id, updated_at DESC) — the sermon-list hot path. The DESC is
    # carried by sa.text() since a plain column list can't express ordering.
    op.create_index(
        "ix_documents_user_updated",
        "documents",
        ["user_id", sa.text("updated_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_documents_user_updated", table_name="documents")
    op.drop_table("documents")
