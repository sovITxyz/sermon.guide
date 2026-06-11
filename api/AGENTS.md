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
- **Every BM25 search MUST include `book_id = ANY(<set>)` in its
  `WHERE`** (Phase 12; ARCHITECTURE.md §3.5). Same invariant as the
  dense arm — the sparse arm is just a different index, the tenant
  scoping rule is identical.
- **Every SQLAlchemy query against `user_library` / `highlights` /
  `collections` MUST filter by `user_id`** derived from the JWT.

## Adding a route

1. New file at `api/<name>.py` exposing `router = APIRouter(prefix=...)`.
2. Protect it with `current_user: CurrentUserDep` unless the route is
   genuinely public (only `/auth/signup`, `/auth/login`, `/healthz`,
   `/readyz` today).
3. Import the session via `session: SessionDep` and `await` everything
   — no sync DB calls inside async handlers.
4. Mount in `main.py`: `app.include_router(name.router)`.
5. Add the route's path to `tests/test_smoke.py::test_healthz_route_is_registered`.
6. If the route takes a JSON body, the request model MUST set
   `model_config = ConfigDict(extra="forbid")` — see the request-model
   posture below.

## Boot guards (`main.py` lifespan, Phase 18)

`main.py` registers a FastAPI `lifespan` hook that runs before the
first request. uvicorn (`make dev`, Docker) and `with TestClient(app):`
execute it; a bare `import main` does NOT — that is what keeps test
collection guard-free, and it is why every boot-time invariant belongs
INSIDE the hook, never at module scope. Phase 19 adds the CORS
prod-origin guard to the same hook.

- **JWT-secret guard.** The process refuses to boot when
  `SERMON_API_JWT_SECRET` is unset/empty or still equals
  `settings.DEV_JWT_SECRET` — the placeholder is public (it lives in
  this repo), so signing with it lets anyone mint a valid token for any
  `user_id`: a total tenant-isolation defeat. The refusal message names
  both env vars so a failed deploy is self-diagnosing.
- **Dev opt-out: `SERMON_API_ENV=dev`.** `ApiSettings.env` is
  `Literal["dev", "prod"]` and **defaults to `"prod"`** — fail closed:
  any environment that does not explicitly declare itself dev gets the
  full guards, and an empty string (compose's `${VAR:-}`) also resolves
  to prod. `make dev` keeps working because `infra/.env` sets
  `SERMON_API_ENV=dev`. Never set `dev` on a deployment that faces real
  users. Defense-in-depth: `infra/docker-compose.prod.yml` additionally
  hard-fails at compose-up when `SERMON_API_JWT_SECRET` is unset.

## Request models: `extra="forbid"` (Phase 18)

Every inbound body model — `SignupRequest`, `LoginRequest`,
`SearchRequest`, `SummaryRequest` — sets
`model_config = ConfigDict(extra="forbid")`: an unknown field (e.g. a
smuggled `user_id` or `book_ids`) is a hard 422 naming the field, never
a silently-dropped key. This makes the tenant rule above mechanical
instead of reviewer-enforced (closes Phase 12 deviation d). New request
models MUST set it; response models don't need it. The `web/` proxies
are unaffected — they rebuild bodies with exact field whitelists. In
tests, exercise it with `Model.model_validate({...})`, not kwargs —
pyright strict already rejects unknown kwargs at type-check time.

## Probes: `/healthz` vs `/readyz`

- `GET /healthz` — liveness only: "is the process alive". Cheap and
  dependency-free; keep it that way.
- `GET /readyz` (`readyz.py`, Phase 18) — readiness: "can it serve real
  traffic". Probes Postgres, Milvus, and Redis concurrently with a ~2 s
  per-dep budget; 200 `{"status": "ready", "deps": {...}}` only when
  all three answer, 503 with per-dep `"down"` otherwise. Failure detail
  goes to the server log, never the body (connection errors can embed
  DSNs and the Redis password). Phase 29 points the container
  HEALTHCHECK here; Phase 30 wires the k8s readinessProbe. Both probe
  routes are genuinely public — no auth, no tenant surface.

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

## Cross-package imports from `worker/`

`api/` reaches into `../worker` for five things only — `db` (since
Phase 7), `embedding.embed`, `inference` (the Phase 16b remote
transport: `embed_texts`, `rerank_scores`, and the exception taxonomy
`main.py` maps to 503/502), `scripts.bootstrap_milvus`'s
`COLLECTION_NAME` + `make_client`, and `retrieval` (the hybrid
dense+sparse+RRF kernel from Phase 12). The api venv accordingly
carries `pymilvus`, `numpy`, `openai`, `httpx`, and `psycopg` (the sync
driver `embedding.py`'s space guard reads its meta row with). Keep
`pymilvus` pinned in lockstep with `worker/pyproject.toml` — drift
surfaces as a wire-protocol mismatch only at runtime; the model-weight
lockstep concern died with Phase 16b (no process loads weights).

## Inference calls made by this process (Phase 16b, ADR 0006)

NO model weights load in this process. Every inference leg is a remote
API call — clients are lazy + cached so import / lint / test cost stays
free, and the empty-library path never needs a key or network:

| File | Transport | Model (env-driven) | Used for |
|------|-----------|--------------------|----------|
| `worker.embedding.embed` | `worker/inference.py` embeddings (OpenAI-compatible) | `BAAI/bge-large-en-v1.5` (`SERMON_EMBEDDINGS_MODEL`) | Query embedding (dense arm). Space-guarded against the Postgres `meta` row. |
| `rerank._score_pairs` | `worker/inference.py` rerank (DeepInfra native shape) | `Qwen/Qwen3-Reranker-8B` (`SERMON_RERANK_MODEL`) | Top-30 → top-N rerank (Phase 13 contract, Phase 16b transport). |
| `highlight._embed_batch` | `worker/inference.py` embeddings | `BAAI/bge-m3` (module constant) | Sentence-level pruning; query + all sentences ride ONE batched call. |

All three are keyed by the unprefixed `DEEPINFRA_API_KEY`. Failure
mapping is centralized in `main.py`: unset key →
`MissingInferenceKeyError` → 503 naming the env var; upstream failure
after the single retry → `RemoteInferenceError` → 502 naming the
provider + leg. The remote calls carry only the query + chunk text the
JWT user was already authorized to read — never `user_id`/JWT/email
(see the tenant note below and `worker/inference.py`).

The `/search-summary` LLM (Phase 14; transport re-cut in Phase 14b,
[ADR 0005](../docs/adr/0005-llm-transport.md)) follows the same shape:
a network call through the `openai` SDK to an OpenAI-compatible
endpoint — Google's compat endpoint by default, ppq.ai via
`SERMON_API_LLM_PROVIDER=ppq`, with `summary.py:_PROVIDERS` as the
single provider map and the unprefixed `GOOGLE_API_KEY` /
`PPQ_API_KEY` as keys. `SERMON_API_LLM_REASONING_EFFORT=none` (Phase
16b) disables Gemini 2.5 Flash thinking on providers that honor it.

The rerank + highlight stages run *after* both retrieval arms' tenant
filters have already executed. They take a `Sequence[RetrievalHit]`
that was filtered by `book_id` upstream, score (query, chunk) pairs,
and return a re-ordered + pruned subset. They **never** query the DB or
Milvus and so introduce no new tenant surface — the remote scorer sees
only chunks the JWT-authenticated user is already authorized to read
(CLAUDE.md tenant rule, ARCHITECTURE.md §3 + §7.1). The Phase 16b
outbound calls carry that authorized text to DeepInfra (zero-retention
default, ADR 0006) with the key in the `Authorization` header only.

What `api/` still **must not** import:

- `worker.celery_app` / `worker.tasks.*` — those pull pandoc, EbookLib,
  pypandoc, pymupdf4llm (the extractor deps), and the Celery worker
  bootstrap. The api process only *enqueues* tasks, never executes them.
- `worker.ingest`, `worker.chunking`, `worker.dedup`, `worker.extractors`
  — same rationale: extraction + ingestion is a worker concern.

## Celery client (`tasks_client.py`)

api/ enqueues ingest tasks by name via a thin `Celery()` instance
against the same Redis broker / backend — `send_task(
"tasks.ingest.ingest_book", args=[…])`. Drift between this module's
`RedisSettings` and `worker/celery_app.py:RedisSettings` is a silent
failure — the api enqueues into a queue the worker isn't reading. If
you change one, change both.

## Open trust gaps

- **No task ownership table.** `GET /tasks/{task_id}` requires auth but
  doesn't check that the caller enqueued the task — the task_id is the
  capability (122-bit Celery UUID, computationally unguessable). Fold in
  an `upload_tasks(task_id, user_id)` row once a later phase needs it;
  Phase 11's `/search` route doesn't touch upload tasks so this stays
  open.
- **Orphan-vector risk from Phase 9.** The Celery pipeline isn't
  crash-safe between Milvus insert and `global_books` commit
  (`worker/AGENTS.md` documents the same caveat). The `/upload` route
  doesn't yet add a task-id-keyed idempotency token.
- **No library cap on the search filter.** A user with 10K books
  produces a ~360 KB `book_id IN [...]` filter expression on every
  `/search`. Phase 12's BM25 arm doubles the per-query work (Milvus
  filter + Postgres `book_id = ANY(...)`). Both backends still accept
  it in practice at v0 scale; introducing a chunked-filter or
  partition-key narrowing strategy is the next phase to do this
  properly.
- **No graceful degradation when retrieval infra fails.** Phase 12
  noted `asyncio.gather` lacks `return_exceptions=True` on the
  dense/sparse fan-out — a Milvus or Postgres blip is still a 500.
  Phase 16b *did* centralize the inference failure mapping (remote
  call fails after retry → 502, key unset → 503, `main.py`), but
  there is no fallback path (e.g. raw-RRF-on-rerank-failure); same
  posture held over to a future ops-resilience pass.
- **Inference wall-time moved off-box (Phase 16b).** The in-process-era
  numbers (~30 s warm rerank on dev CPU; Phase 14b: warm
  `/search-summary` E2E ≈ 134 s = ~71–76 s retrieval/rerank/highlight +
  ~58–64 s thinking-enabled LLM) are obsolete — embeddings/rerank/
  highlight are now sub-second-class provider calls, and
  `SERMON_API_LLM_REASONING_EFFORT=none` collapses the LLM leg on
  providers that honor it. The remaining structural latency is network
  round-trips (4 sequential inference legs per summary: embed → rerank
  → highlight → LLM). See the Phase 16b row in docs/PHASES.md for the
  measured before/after.

## Before merging anything in this directory

Run all three when the change touches a DB query, Milvus query, auth
dependency, or request handler that takes user input:

- `/check-tenant-leak` — grep audit (`.claude/skills/check-tenant-leak`).
- `tenant-auditor` subagent — semantic audit (`.claude/agents/tenant-auditor.md`).
- `/security-review` — built-in Claude skill for OWASP-class issues.

For migration-touching PRs, also run the `schema-reviewer` subagent
(see `worker/AGENTS.md`).
