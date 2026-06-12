"""Phase 32 — reading_positions: per-(user, book) reader resume point.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-11

Adds one table:

- ``reading_positions(position_id, user_id, book_id, chunk_index,
  offset_ratio, updated_at)`` — one row per user per book (B1 shape).
  ``PUT /books/{book_id}/position`` upserts ON CONFLICT against
  ``uq_reading_positions_user_book``; ``GET /library`` joins it for
  per-book progress, ON (user_id AND book_id) — never book_id alone,
  which would leak another tenant's position for a shared deduped book.

``offset_ratio`` is nullable double precision (0.0–1.0, validated at the
API layer per the in-phase decision to keep it in the first cut). Both FKs
cascade like ``highlights``: a position is meaningless once its user or
book is gone, and ``user_library``'s RESTRICT FK still guards owned books
from deletion. No extra index — the unique constraint's backing index
covers the doubly-scoped lookups and the per-user join prefix, and
``chunks(book_id)`` already has ``ix_chunks_book_id`` from 0002, so the
windowed-read path needs nothing new here.

Locking: brand-new table — no rewrite or scan of populated tables; the FKs
only take brief SHARE ROW EXCLUSIVE locks on ``users`` and ``global_books``
for the catalog change, safe at this deployment's size.

Hand-written (same convention as 0001–0004).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reading_positions",
        sa.Column("position_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("book_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("offset_ratio", sa.Float(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_reading_positions_user_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["global_books.book_id"],
            name="fk_reading_positions_book_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("user_id", "book_id", name="uq_reading_positions_user_book"),
    )


def downgrade() -> None:
    op.drop_table("reading_positions")
