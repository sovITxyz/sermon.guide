# api/ — agent instructions

Per-package conventions for the FastAPI HTTP layer. See repo-root
[`AGENTS.md`](../AGENTS.md) for cross-package rules and
[`ARCHITECTURE.md`](../ARCHITECTURE.md) for system design. The schema
lives in [`worker/db/`](../worker/db/) — `api/` imports it (the **only**
cross-package import; see the dep-direction rule in the root
`CLAUDE.md`).

## Toolchain

Same uv + ruff (strict select) + pyright (strict) stack as `worker/`.
From `api/`:

```bash
uv sync --all-extras --dev
uv run <tool>     # ruff, pyright, pytest, uvicorn
```

`make dev` boots uvicorn with `PYTHONPATH=../worker` so `from db import …`
resolves at runtime; pytest gets the same path via
`[tool.pytest.ini_options].pythonpath`, and pyright via `extraPaths`.
`make lint` / `format` / `format-check` / `typecheck` / `test` mirror
the worker targets.

## Tenant invariant (load-bearing)

This package is the trust boundary. Every rule below is also enforced
mechanically by `tenant-auditor` and `/check-tenant-leak`; the human
rules are here so the audit isn't your first line of defense.

- **`user_id` ALWAYS comes from the JWT** (`current_user.user_id` via
  the `CurrentUserDep` annotation). Never read it from the request body,
  query params, or path. A new route that takes `user_id: UUID` as input
  is an automatic reject.
- **The user's `book_id` set is resolved server-side** from
  `user_library` for `current_user.user_id` on every search. Never
  accept a `book_ids: list[UUID]` field from the client — the client
  could (deliberately or accidentally) widen its own scope.
- **Every Milvus search MUST include `book_id IN (<set>)` in `expr`.**
  An unfiltered search returns vectors across the whole platform — a
  CVE-class data leak (see `ARCHITECTURE.md` §3 and §7.1).
- **Every SQLAlchemy query against `user_library` / `highlights` /
  `collections` MUST filter by `user_id`** derived from the JWT.

## Adding a route

1. New file at `api/<name>.py` exposing `router = APIRouter(prefix=...)`.
2. Protect it with `current_user: CurrentUserDep` unless the route is
   genuinely public (only `/auth/signup`, `/auth/login`, `/healthz`
   today).
3. Import the session via `session: SessionDep` and `await` everything
   — no sync DB calls inside async handlers.
4. Mount in `main.py`: `app.include_router(name.router)`.
5. Add the route's path to `tests/test_smoke.py::test_healthz_route_is_registered`.

## Auth helpers (`auth.py`)

- `CurrentUserDep` — `Annotated[User, Depends(get_current_user)]`. Decodes
  the bearer token, loads the row, raises a uniformly-shaped 401 on any
  failure (missing header, bad sig, expired, unknown sub, deleted user).
  Differentiating those leaks information to attackers.
- `SessionDep` — `Annotated[AsyncSession, Depends(_session)]`. Yields a
  single `AsyncSession` for the request lifetime; FastAPI handles
  cleanup.
- bcrypt input cap (72 bytes) is enforced in `_hash_password` so two
  long passwords sharing a prefix don't look equivalent. Don't bypass it.

## Common 401/403 mistakes

- **Returning 401 for a missing resource.** Use 404 when the
  authenticated user can't see something because it doesn't exist; 401
  is for "the JWT itself didn't validate". Conflating them confuses
  legitimate clients.
- **Returning 403 instead of 404 for cross-tenant access.** A 403 leaks
  that the resource exists under another tenant. Return 404 — the
  authenticated user cannot see this row, full stop.
- **Forgetting `WWW-Authenticate: Bearer` on a 401.** Curl + many HTTP
  clients won't retry without it. `auth.py` always sets it.

## Celery client (`tasks_client.py`)

api/ never imports `worker.celery_app` or `worker.tasks.*` directly —
that would pull pymilvus, sentence-transformers, pandoc, and the rest of
the worker dep tree into the api venv. Instead, `tasks_client.py`
constructs a thin `Celery()` instance against the same Redis broker /
backend and uses `send_task("tasks.ingest.ingest_book", args=[…])` by
name. Drift between this module's `RedisSettings` and
`worker/celery_app.py:RedisSettings` is a silent failure — the api
enqueues into a queue the worker isn't reading. If you change one, change both.

## Phase 10 follow-ups (open trust gaps)

These are known limitations to address in Phase 11+ as the surface grows:

- **No task ownership table.** `GET /tasks/{task_id}` requires auth but
  doesn't check that the caller enqueued the task — the task_id is the
  capability (122-bit Celery UUID, computationally unguessable). When
  Phase 11 lands `user_library` writes from the search path, fold in an
  `upload_tasks(task_id, user_id)` row and check it.
- **Orphan-vector risk from Phase 9.** The Celery pipeline isn't
  crash-safe between Milvus insert and `global_books` commit
  (`worker/AGENTS.md` documents the same caveat). The `/upload` route
  doesn't yet add a task-id-keyed idempotency token — `/security-review`
  has flagged this as a Phase 11 task.

## Before merging anything in this directory

Run all three when the change touches a DB query, Milvus query, auth
dependency, or request handler that takes user input:

- `/check-tenant-leak` — grep audit (`.claude/skills/check-tenant-leak`).
- `tenant-auditor` subagent — semantic audit (`.claude/agents/tenant-auditor.md`).
- `/security-review` — built-in Claude skill for OWASP-class issues.

For migration-touching PRs, also run the `schema-reviewer` subagent
(see `worker/AGENTS.md`).
