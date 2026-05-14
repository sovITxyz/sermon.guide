"""Test-enqueue helper for the Phase 9 Celery ingest task.

Drives the ``make enqueue FILE=... TENANT=...`` Makefile target. Not
production code — the real producer is the Phase 10 FastAPI ``/upload``
route, which derives ``user_id`` from the request's JWT rather than
from a CLI flag.

## Tenant resolution

The verify step in ``docs/PHASES.md`` uses the literal ``TENANT=tenant_a``,
which isn't a UUID. Two cases:

- *value parses as a UUID* — treat it as a real ``users.user_id``;
  enqueue against it as-is. The caller is responsible for ensuring the
  row exists.
- *value is a string label* (the docs case) — derive a deterministic
  ``uuid5(NAMESPACE_DNS, "<label>.tenants.sermon.guide.local")``, then
  upsert a ``users`` row keyed on it so the FK from ``user_library``
  resolves once the task lands. The email is a synthetic
  ``<label>@tenants.sermon.guide.local`` so two test labels never
  collide on the ``users.email`` unique constraint.

The label path is for local verify only — production never invents
users from a string.
"""

# `@app.task` turns ingest_book into a Celery Task instance (`.delay`,
# `.apply_async`, etc.) but pyright still sees the underlying function.
# pyright: reportFunctionMemberAccess=false

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert as pg_insert

from db import User, get_sync_session_factory
from tasks.ingest import ingest_book  # also imports celery_app, registering the task

_LABEL_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-000000000001")
_LABEL_EMAIL_DOMAIN = "tenants.sermon.guide.local"


def _resolve_tenant(value: str) -> uuid.UUID:
    """Map *value* to a ``users.user_id`` and ensure the row exists."""
    try:
        return uuid.UUID(value)
    except ValueError:
        pass
    user_id = uuid.uuid5(_LABEL_NAMESPACE, f"{value}.{_LABEL_EMAIL_DOMAIN}")
    sf = get_sync_session_factory()
    with sf() as session, session.begin():
        stmt = (
            pg_insert(User)
            .values(
                user_id=user_id,
                email=f"{value}@{_LABEL_EMAIL_DOMAIN}",
                password_hash="bcrypt$enqueue-test",  # noqa: S106 — local-dev test seed
            )
            .on_conflict_do_nothing(index_elements=["user_id"])
        )
        session.execute(stmt)
    return user_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="enqueue_ingest",
        description="Enqueue a single ingest task against the Phase 9 Celery worker.",
    )
    parser.add_argument(
        "path",
        type=Path,
        help="Absolute path to an EPUB/PDF (the worker must see the same path).",
    )
    parser.add_argument(
        "--tenant",
        required=True,
        help="users.user_id UUID, or a label (uuid5-derived + upserted into users).",
    )
    args = parser.parse_args(argv)

    if not args.path.is_absolute():
        args.path = args.path.resolve()
    if not args.path.exists():
        sys.stderr.write(f"FILE not found: {args.path}\n")
        return 2

    user_id = _resolve_tenant(args.tenant)
    async_result = ingest_book.delay(str(args.path), str(user_id))
    sys.stdout.write(
        f"Enqueued ingest task_id={async_result.id} path={args.path} user_id={user_id}\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
