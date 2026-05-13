"""sermon.guide Postgres schema + async session factory.

This package is the shared DB layer; ``api/`` imports it directly (see the
dependency-direction rule in the root ``CLAUDE.md``). Tables live at
``ARCHITECTURE.md`` §4 — keep this module in lockstep with that section.
"""

from db.models import (
    Base,
    Collection,
    GlobalBook,
    Highlight,
    User,
    UserLibraryEntry,
)
from db.session import get_engine, get_session_factory

__all__ = [
    "Base",
    "Collection",
    "GlobalBook",
    "Highlight",
    "User",
    "UserLibraryEntry",
    "get_engine",
    "get_session_factory",
]
