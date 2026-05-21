"""Phase 12 — chunks table + tsvector GIN for the BM25 arm of hybrid retrieval.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-21

ARCHITECTURE.md §3.5 + §4; ADR 0004. Adds one new table:

- ``chunks(chunk_id, book_id, chunk_index, content, parent_section,
  filename, tsv, created_at)`` — one row per ingested chunk; mirrors the
  ``content_chunk`` + ``metadata.chunk_index`` Milvus already carries.

``tsv`` is a PostgreSQL ``GENERATED ALWAYS AS ... STORED`` column — kept
in sync with ``content`` by the DB, never written by the application.
The GIN index over ``tsv`` is what makes ``tsv @@ websearch_to_tsquery(...)``
fast at corpus scale. The B-tree on ``book_id`` lets the planner combine
the tenant filter (``book_id = ANY(...)``) with the GIN scan via a
bitmap heap scan.

Hand-written (same convention as 0001): autogenerate misses ``Computed``
on the ``tsv`` column and reorders the index dialect args.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chunks",
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("book_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("parent_section", sa.Text(), nullable=True),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', content)", persisted=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["global_books.book_id"],
            name="fk_chunks_book_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("book_id", "chunk_index", name="uq_chunks_book_chunk"),
    )
    op.create_index("ix_chunks_book_id", "chunks", ["book_id"])
    op.create_index(
        "ix_chunks_tsv_gin",
        "chunks",
        ["tsv"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_tsv_gin", table_name="chunks")
    op.drop_index("ix_chunks_book_id", table_name="chunks")
    op.drop_table("chunks")
