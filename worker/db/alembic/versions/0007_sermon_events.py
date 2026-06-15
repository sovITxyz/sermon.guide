"""Phase 38 — sermon_events: user-owned preaching calendar (B3 slice).

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-15

Adds one table — the server half of the B3 calendar:

- ``sermon_events(event_id, user_id, event_date, title, series,
  document_id, created_at, updated_at)`` — one row per dated calendar
  entry. ``api/calendar.py`` does range-GET / POST (+ optional weekly
  materializer) / partial-PATCH / DELETE, all DOUBLE-scoped (``event_id``
  AND ``user_id``); a non-owned ``event_id`` is a uniform 404 with no
  existence oracle (the Phase 20 ``/tasks`` posture).

``event_date`` is a Postgres DATE, NOT a timestamptz — preaching is
day-anchored, and a UTC-midnight timestamptz silently shifts a day for
UTC-minus users. Dates stay ``YYYY-MM-DD`` end-to-end. This is the
schema's first DATE column (``sa.Date()``, no timezone, no server_default).

``series`` is an optional free-text recurrence label (B3 — NOT an RRULE);
the weekly materializer writes INDEPENDENT rows (no parent linkage), so
each materialized occurrence PATCHes / DELETEs on its own.

Two FKs:

- ``user_id`` -> ``users.user_id`` ON DELETE CASCADE (like every
  user-owned table — events are meaningless once their user is gone).
- ``document_id`` -> ``documents.document_id`` ON DELETE SET NULL — the
  schema's first SET NULL. Deleting the linked document detaches the event
  instead of cascading it away; the documents API soft-deletes (row + link
  survive), so the SET NULL is the defensive behaviour for a real row
  delete. Tenancy on ``document_id`` is enforced by the API ownership
  check (attacker-controlled body input), not the FK.

The ``(user_id, event_date)`` index serves the half-open range scan; the
``user_id`` prefix scopes per tenant and ``event_date`` is bidirectional
so a plain ascending column list suffices (no DESC trick). There is
DELIBERATELY no unique on ``(user_id, event_date)`` — two services on one
Sunday is normal — so this is an Index, not a UniqueConstraint.

``updated_at`` carries ``server_default=func.now()`` for the insert but NO
``onupdate`` (the schema-wide convention): the API bumps it EXPLICITLY per
PATCH.

Locking: brand-new table — no rewrite or scan of populated tables; the FKs
take only brief SHARE ROW EXCLUSIVE locks on ``users`` and ``documents``
for the catalog change, safe at this deployment's size.

Hand-written (same convention as 0001–0006). First use of ``sa.Date()``
and the first ``ondelete="SET NULL"`` FK in the schema.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sermon_events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Day-anchored DATE (not timestamptz) — see module docstring.
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("series", sa.Text(), nullable=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
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
            name="fk_sermon_events_user_id",
            ondelete="CASCADE",
        ),
        # First ON DELETE SET NULL in the schema: detach the event when its
        # linked document row is truly deleted (the documents API
        # soft-deletes, so this fires only on a real row delete).
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.document_id"],
            name="fk_sermon_events_document_id",
            ondelete="SET NULL",
        ),
    )
    # (user_id, event_date) — the half-open range hot path. Plain ascending
    # column list (no DESC); DELIBERATELY an Index, not a UniqueConstraint
    # (two services one Sunday is normal).
    op.create_index(
        "ix_sermon_events_user_date",
        "sermon_events",
        ["user_id", "event_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_sermon_events_user_date", table_name="sermon_events")
    op.drop_table("sermon_events")
