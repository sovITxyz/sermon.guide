"""Phase 51 — search_history: saved /search-summary runs for the Recent panel.

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-29

Adds one table — the persistence half of the "Recent" panel on ``/search``
(``api/search_history.py`` + the save point in ``api/summary.py``). A summary
search is the platform's most expensive request (the 4-leg embed → rerank →
highlight → LLM pipeline, 2–4 min wall time), so each successful run is saved
WHOLE so the user can reopen it and the saved ``result`` blob renders instantly
without re-running (and re-paying for) the pipeline.

- ``search_history(history_id, user_id, query, scope_book_ids,
  scope_collection_ids, result, created_at)`` — one row per saved summary
  search. User-owned like ``documents`` / ``sermon_events``.

``query`` is the natural-language question saved verbatim (the user's OWN
history — unlike the Phase 27 metrics path, which scrubs query text).
``scope_book_ids`` / ``scope_collection_ids`` are the Phase 49 scope the search
ran under (book / collection UUIDs as text), JSONB, ``NOT NULL`` with
``server_default '[]'``. ``result`` is the serialized ``SummaryResponse``
(``summary`` + ``citations`` + ``degraded``), JSONB, ``NOT NULL`` — the whole
replayable blob.

The FK -> ``users.user_id`` is ON DELETE CASCADE — a saved search is
meaningless once its user is gone. ``created_at`` carries
``server_default=func.now()``; there is NO ``updated_at`` — a saved search is
an immutable row (the ``sermon_doc_revisions`` precedent).

``ix_search_history_user_created (user_id, created_at DESC)`` backs the panel's
newest-first per-user list AND the per-user retention-cap prune (newest-N kept;
``api/summary.py`` deletes older rows in the same transaction as the insert).

Locking: brand-new table — no rewrite or scan of populated tables; the FK takes
only a brief SHARE ROW EXCLUSIVE lock on ``users`` for the catalog change, safe
at this deployment's size (the 0007–0012 note).

Hand-written (same convention as 0001–0012).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "search_history",
        sa.Column("history_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column(
            "scope_book_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "scope_collection_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_search_history_user_id",
            ondelete="CASCADE",
        ),
    )
    # Newest-first per-user list (the panel hot path) + the retention-cap prune.
    # DESC on created_at matches the ORDER BY so the planner walks it in order.
    op.create_index(
        "ix_search_history_user_created",
        "search_history",
        ["user_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_search_history_user_created", table_name="search_history")
    op.drop_table("search_history")
