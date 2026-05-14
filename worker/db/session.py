"""SQLAlchemy engine + session factories — async (FastAPI) and sync (worker).

Module-level singletons; first call lazily constructs each engine against
``DBSettings`` from ``db.settings``. Tests and Alembic build their own
engines with explicit URLs and should not touch the globals here.

Two engines coexist because the codebase has two consumers with different
runtime models:

- ``api/`` (Phase 10+) is FastAPI — async end to end. Use
  ``get_session_factory()``.
- ``worker/`` ingest (Phase 8+) and Celery tasks (Phase 9) are
  synchronous; bridging via ``asyncio.run`` would leave loop-bound
  connections stale in the async pool between calls. Use
  ``get_sync_session_factory()`` from sync code paths.

Both share the same ``DBSettings`` so connection params stay aligned;
only the driver differs (``asyncpg`` vs ``psycopg``).
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from db.settings import settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

_sync_engine: Engine | None = None
_sync_session_factory: sessionmaker[Session] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(settings.dsn, future=True)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
        )
    return _session_factory


def _sync_dsn() -> str:
    """Sync DSN built from ``DBSettings`` — psycopg3 driver."""
    s = settings
    return f"postgresql+psycopg://{s.user}:{s.password}@{s.host}:{s.port}/{s.db}"


def get_sync_engine() -> Engine:
    global _sync_engine
    if _sync_engine is None:
        _sync_engine = create_engine(_sync_dsn(), future=True)
    return _sync_engine


def get_sync_session_factory() -> sessionmaker[Session]:
    global _sync_session_factory
    if _sync_session_factory is None:
        _sync_session_factory = sessionmaker(
            get_sync_engine(),
            expire_on_commit=False,
        )
    return _sync_session_factory
