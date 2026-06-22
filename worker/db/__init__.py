"""sermon.guide Postgres schema + session factories.

This package is the shared DB layer; ``api/`` imports it directly (see the
dependency-direction rule in the root ``CLAUDE.md``). Tables live at
``ARCHITECTURE.md`` §4 — keep this module in lockstep with that section.
"""

from db.models import (
    Base,
    Chunk,
    Collection,
    Document,
    EditorLink,
    GlobalBook,
    Highlight,
    Meta,
    OAuthConnection,
    ReadingPosition,
    SermonDocRevision,
    SermonEvent,
    UploadTask,
    User,
    UserLibraryEntry,
)
from db.session import (
    get_engine,
    get_session_factory,
    get_sync_engine,
    get_sync_session_factory,
)

__all__ = [
    "Base",
    "Chunk",
    "Collection",
    "Document",
    "EditorLink",
    "GlobalBook",
    "Highlight",
    "Meta",
    "OAuthConnection",
    "ReadingPosition",
    "SermonDocRevision",
    "SermonEvent",
    "UploadTask",
    "User",
    "UserLibraryEntry",
    "get_engine",
    "get_session_factory",
    "get_sync_engine",
    "get_sync_session_factory",
]
