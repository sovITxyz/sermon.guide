"""Postgres connection settings loaded from environment.

Reads ``SERMON_POSTGRES_*`` per ``infra/AGENTS.md`` naming. ``infra/.env``
holds the local-dev defaults; Make targets source it before invoking Python,
so a missing variable means an unsourced env, not a missing default.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class DBSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SERMON_POSTGRES_", extra="ignore")

    host: str = "localhost"
    port: int = 54322
    user: str = "sermon"
    password: str = "sermon_local_dev"  # noqa: S105 — matches infra/.env local-dev default
    db: str = "sermon"

    @property
    def dsn(self) -> str:
        """Async DSN for SQLAlchemy + asyncpg."""
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}"


settings = DBSettings()
