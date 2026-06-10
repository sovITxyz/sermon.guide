"""Phase 16b — meta table + embedding-space pin (ADR 0006).

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-05

Adds one tiny key/value table:

- ``meta(key, value, updated_at)`` — deployment-scoped facts.

and seeds the one row Phase 16b needs:

- ``('embedding_model_id', 'BAAI/bge-large-en-v1.5')`` — the model whose
  vectors populate Milvus today. ``worker/embedding.py`` refuses to embed
  when ``SERMON_EMBEDDINGS_MODEL`` disagrees with this row, so a silent
  provider/model drift (which would mix embedding spaces and quietly
  destroy retrieval) fails loudly instead. Seeding in the migration is
  deliberate: every existing deployment's vectors ARE bge-large-en-v1.5,
  and a fresh deployment that wants a different embedder must make that
  choice explicitly (update this row + bootstrap an empty collection),
  never implicitly via env.

Hand-written (same convention as 0001/0002).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "meta",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # ON CONFLICT DO NOTHING is vacuous today (the table is created empty two
    # statements up) — defense-in-depth for the day this seed is ever decoupled
    # from the create_table (schema-reviewer 2026-06-05 hardening note).
    op.execute(
        "INSERT INTO meta (key, value) "
        "VALUES ('embedding_model_id', 'BAAI/bge-large-en-v1.5') "
        "ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    op.drop_table("meta")
