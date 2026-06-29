"""Phase 50 — per-sermon citation scope: documents.scope_book_ids/_collection_ids.

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-29

Adds two JSONB columns to ``documents`` so a sermon remembers the book /
collection scope its citation drawer ("Cite from your library") is limited to
while writing — the choice survives reload and device (Phase 50, "same page").

- ``scope_book_ids`` — JSONB array of the ad-hoc book UUIDs (as text) the
  sermon's citation search is scoped to.
- ``scope_collection_ids`` — JSONB array of the collection UUIDs (as text) the
  sermon's citation search is scoped to.

Both are ``NOT NULL`` with ``server_default '[]'`` (empty = whole library, the
backward-compatible default). The blob is tiny and read/written WHOLE with the
doc — it is never queried by "which sermons use book X" — so a JSONB array
beats a membership join table; it rides the existing ``base_updated_at``
optimistic-concurrency PATCH. The API clamps each set to the JWT user's library
/ owned collections on every write (``api/documents.py``), so a persisted scope
can never name a book or collection the user does not own.

``add_column`` with a ``server_default`` backfills existing rows to ``'[]'``
WITHOUT a table rewrite (Postgres records the default in the catalog and
materializes it lazily on read for pre-existing rows), so this is safe on a
populated ``documents`` table.

Hand-written (same convention as 0001-0011).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default '[]' backfills existing rows without a rewrite (the default
    # is recorded in the catalog; pre-existing rows read it lazily).
    op.add_column(
        "documents",
        sa.Column(
            "scope_book_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "scope_collection_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("documents", "scope_collection_ids")
    op.drop_column("documents", "scope_book_ids")
