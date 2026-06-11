"""Phase 20 — upload_tasks: task ownership + in-flight idempotency claim.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-11

Adds one table:

- ``upload_tasks(task_id, user_id, book_id, filename, created_at)`` — one
  row per ``POST /upload``. ``task_id`` is the Celery task UUID, minted by
  the api and committed BEFORE ``send_task`` so a crash between the two can
  never produce a running task its owner cannot see. ``GET /tasks/{task_id}``
  authorizes against this row (non-owned and nonexistent are both 404),
  replacing the Phase 10 capability-UUID posture. ``book_id`` is the worker's
  in-flight new-book claim — written before the first non-transactional write
  so a redelivery after a mid-window crash can scrub partial Milvus vectors
  and re-run under the same book_id (closes the Phase 9 orphan-vector
  window for api-enqueued tasks).

``book_id`` deliberately has NO FK to ``global_books``: the claim exists to
name a book whose ``global_books`` row may never land (that is the crash
window it reconciles). ``user_id`` cascades with the owning user, same as
every user-owned table.

Locking: brand-new table — no rewrite or scan of populated tables; the FK
to ``users`` only takes a brief SHARE ROW EXCLUSIVE on ``users`` for the
catalog change, safe at this deployment's size.

Hand-written (same convention as 0001–0003).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "upload_tasks",
        sa.Column("task_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("book_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_upload_tasks_user_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_upload_tasks_user_id", "upload_tasks", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_upload_tasks_user_id", table_name="upload_tasks")
    op.drop_table("upload_tasks")
