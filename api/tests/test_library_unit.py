"""Unit test pinning the ``GET /library`` tenant filter — no DB.

The live query is exercised end-to-end by the Phase 15 API verify; this
file ensures the WHERE clause stays scoped to the JWT-derived ``user_id``
and the join to ``global_books`` is present, so a refactor can't silently
widen the listing to every tenant's books (CLAUDE.md tenant invariant).
"""

# ``_library_stmt`` is intentionally private; compiled-statement metadata
# (``.params``) is loosely typed under pyright strict.
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false

from __future__ import annotations

import uuid

from sqlalchemy.dialects import postgresql

from library import _library_stmt


def _compiled_sql(user_id: uuid.UUID) -> str:
    return str(_library_stmt(user_id).compile(dialect=postgresql.dialect()))


def test_library_stmt_filters_by_user_id() -> None:
    uid = uuid.uuid4()
    compiled = _library_stmt(uid).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    # The tenant gate: listing is scoped to user_library.user_id, and the
    # only bound value is the JWT-derived user_id we passed in.
    assert "user_library.user_id =" in sql
    assert uid in compiled.params.values()


def test_library_stmt_joins_global_books() -> None:
    # Title/author come from the shared global_books row; without the join
    # the listing can't render book names.
    sql = _compiled_sql(uuid.uuid4())
    assert "global_books" in sql
    assert "user_library" in sql


def test_library_stmt_orders_newest_first() -> None:
    sql = _compiled_sql(uuid.uuid4())
    assert "ORDER BY user_library.added_at DESC" in sql
