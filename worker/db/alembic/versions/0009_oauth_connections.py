"""Phase 44 — oauth_connections: encrypted OAuth refresh-token vault.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-20

Adds one table — the storage half of the B4 OAuth vault (Phase 44). The
``/integrations/{provider}/callback`` route exchanges a Google authorization
code for tokens, then UPSERTs one row here per ``(user_id, provider)``. The
refresh token (and the optional short-lived access token) are stored ONLY as
AES-256-GCM ciphertext — the api-side ``crypto_vault`` module encrypts before
write and decrypts on use; the database never holds plaintext token material.
The ONLY token-derived value ever returned to the browser is
``provider_account_email`` (fetched from the userinfo endpoint).

- ``oauth_connections(id, user_id, provider, provider_account_email,
  refresh_token_ciphertext, access_token_ciphertext, token_expiry, scopes,
  created_at, updated_at)`` — one row per (user, provider) connection.
  ``refresh_token_ciphertext`` / ``access_token_ciphertext`` are BYTEA with
  the AESGCM layout ``nonce(12 bytes) || ciphertext+tag`` (the per-encryption
  random 96-bit nonce is PREPENDED). ``provider`` is generic text ('google'
  now; 'microsoft' in Phase 46). ``scopes`` is the space-delimited granted
  scope string Google returns. ``token_expiry`` is the stored access token's
  expiry (NULLABLE — the vault strictly needs only the refresh token).

``user_id`` carries an FK -> ``users.user_id`` ON DELETE CASCADE — a
connection is meaningless once its user is gone. The tenant gate at the API
layer ALWAYS filters by the JWT-derived ``user_id`` (CLAUDE.md), never from
request input.

The ``UNIQUE(user_id, provider)`` constraint backs the callback's
``ON CONFLICT(user_id, provider) DO UPDATE`` (reconnect overwrites the row in
place, yielding a fresh refresh token) AND serves the per-user list scan.

``updated_at`` carries the schema-wide ``server_default=func.now()`` for the
insert but has NO ``onupdate`` (the convention): the upsert bumps it
EXPLICITLY via ``func.now()`` (the ``documents`` / ``sermon_events``
precedent).

Locking: brand-new table — no rewrite or scan of populated tables; the FK
takes only a brief SHARE ROW EXCLUSIVE lock on ``users`` for the catalog
change, safe at this deployment's size (same note as 0008).

Hand-written (same convention as 0001–0008).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Generic provider key — 'google' now, 'microsoft' in Phase 46.
        sa.Column("provider", sa.Text(), nullable=False),
        # The ONLY token-derived value ever returned to the browser (fetched
        # from the userinfo endpoint).
        sa.Column("provider_account_email", sa.Text(), nullable=False),
        # AESGCM ciphertext: 12-byte nonce PREPENDED to ciphertext+tag. Never
        # plaintext. The refresh token is the vault's strict requirement.
        sa.Column("refresh_token_ciphertext", postgresql.BYTEA(), nullable=False),
        # Optional short-lived access token (same nonce-prepended layout) —
        # storing it avoids a refresh round-trip on the first Phase 45 call.
        sa.Column("access_token_ciphertext", postgresql.BYTEA(), nullable=True),
        # Expiry of the stored access token (NULLABLE).
        sa.Column("token_expiry", sa.DateTime(timezone=True), nullable=True),
        # Space-delimited granted scope string returned by Google.
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # No ``onupdate`` (schema-wide convention) — the upsert bumps this
        # explicitly via func.now() (the documents / sermon_events precedent).
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_oauth_connections_user_id",
            ondelete="CASCADE",
        ),
        # Backs ON CONFLICT(user_id, provider) DO UPDATE (reconnect overwrites
        # in place) AND serves the per-user list scan.
        sa.UniqueConstraint(
            "user_id",
            "provider",
            name="uq_oauth_connections_user_provider",
        ),
    )


def downgrade() -> None:
    op.drop_table("oauth_connections")
