"""Phase 48 — collection_books: book membership in a user's collection.

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-29

Adds one table — the join half of the B-library collections feature
(``api/collections_routes.py``). The dormant ``collections`` table (migration
0001) gains a membership table so books get an organizational home.

- ``collection_books(collection_book_id, collection_id, book_id, user_id,
  added_at)`` — one row per (collection, book) pairing. Mirrors
  ``user_library`` (a per-user membership keyed on ``book_id``) but carries a
  ``collection_id`` and a DENORMALIZED ``user_id``.

``user_id`` is DENORMALIZED — duplicated from the owning ``collections`` row
(like ``editor_links`` / ``sermon_doc_revisions``) — so the tenant gate filters
memberships by the JWT-derived ``user_id`` WITHOUT a join back to
``collections``. It carries its own FK -> ``users.user_id`` ON DELETE CASCADE.

All three FKs are ON DELETE CASCADE — a membership is meaningless once its
collection, its book, or its user is gone. ``book_id`` -> ``global_books`` is
CASCADE (unlike ``user_library``'s RESTRICT) because the membership is a pure
organizational pointer; the dedup invariant that keeps a shared book alive
lives on ``user_library``, not here.

``UniqueConstraint(collection_id, book_id)`` backs the add-books
``ON CONFLICT (collection_id, book_id) DO NOTHING`` idempotency (re-adding a
book already in the collection is a no-op) and forbids duplicate memberships.
``ix_collection_books_user_book (user_id, book_id)`` serves the denormalized
tenant gate's doubly-scoped lookups.

``added_at`` carries ``server_default=func.now()`` for the insert; there is no
``updated_at`` — a membership is an immutable pairing.

Locking: brand-new table — no rewrite or scan of populated tables; the three
FKs take only brief SHARE ROW EXCLUSIVE locks on ``collections``,
``global_books``, and ``users`` for the catalog change, safe at this
deployment's size (the 0007–0010 note).

Hand-written (same convention as 0001–0010).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collection_books",
        sa.Column("collection_book_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("book_id", postgresql.UUID(as_uuid=True), nullable=False),
        # DENORMALIZED owner — duplicated from the collections row so the tenant
        # gate filters here without a join back to collections. See module
        # docstring.
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["collections.collection_id"],
            name="fk_collection_books_collection_id",
            ondelete="CASCADE",
        ),
        # CASCADE (unlike user_library's RESTRICT) — a membership is a pure
        # organizational pointer; the dedup-keep-alive invariant lives on
        # user_library.
        sa.ForeignKeyConstraint(
            ["book_id"],
            ["global_books.book_id"],
            name="fk_collection_books_book_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_collection_books_user_id",
            ondelete="CASCADE",
        ),
        # Backs ON CONFLICT (collection_id, book_id) DO NOTHING (idempotent
        # re-add) AND forbids duplicate memberships.
        sa.UniqueConstraint(
            "collection_id",
            "book_id",
            name="uq_collection_books_collection_book",
        ),
    )
    # Doubly-scoped (user_id AND book_id) lookups for the denormalized tenant
    # gate.
    op.create_index(
        "ix_collection_books_user_book",
        "collection_books",
        ["user_id", "book_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_collection_books_user_book", table_name="collection_books")
    op.drop_table("collection_books")
