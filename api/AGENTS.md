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
7. If the route is public or expensive (provider tokens, long-held
   connections, heavy CPU), give it a rate-limit bucket — see "Rate
   limiting" below for the three-line recipe.

## Boot guards (`main.py` lifespan, Phases 18–19)

`main.py` registers a FastAPI `lifespan` hook that runs before the
first request. uvicorn (`make dev`, Docker) and `with TestClient(app):`
execute it; a bare `import main` does NOT — that is what keeps test
collection guard-free, and it is why every boot-time invariant belongs
INSIDE the hook, never at module scope.

- **JWT-secret guard (Phase 18).** The process refuses to boot when
  `SERMON_API_JWT_SECRET` is unset/empty or still equals
  `settings.DEV_JWT_SECRET` — the placeholder is public (it lives in
  this repo), so signing with it lets anyone mint a valid token for any
  `user_id`: a total tenant-isolation defeat. The refusal message names
  both env vars so a failed deploy is self-diagnosing.
- **CORS prod-origin guard (Phase 19).** `main.py` pairs
  `allow_origins=settings.cors_origins` with `allow_credentials=True`;
  Starlette mirrors the request Origin back for a `"*"` entry, which is
  credentials-for-any-site. Outside dev the process refuses to boot when
  the list is empty/unset or contains a wildcard, an empty string, or a
  loopback origin (`localhost`/`127.0.0.1`/`0.0.0.0`/`::1` — a leftover
  dev default means the operator never set the real origin). Prod must
  set `SERMON_API_CORS_ORIGINS` to the exact browser origin(s) as a JSON
  list; the prod compose hard-fails without it. The middleware is
  constructed at import time, so the guard validates `settings` at
  lifespan time — tests monkeypatch settings attributes, never env vars.
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

## Rate limiting (`ratelimit.py`, Phase 19)

**Choice: a small hand-rolled FastAPI dependency over the already-locked
`redis.asyncio` client — NOT slowapi/fastapi-limiter.** Rationale: (1)
zero new dependencies (`redis` 6.x is already a direct dep; slowapi and
friends would change `uv.lock`); (2) slowapi's incomplete typing and
decorator/`Request` coupling fight pyright strict, while the primitive we
need is one atomic pipeline (`INCR` + first-hit `EXPIRE NX` + `TTL`);
(3) dependencies compose with `CurrentUserDep` for per-user keying,
which a pure middleware can't do without re-decoding the JWT.

This is the SECOND layer. Caddy already rate-limits per-IP at the edge
(`infra/caddy/Caddyfile`: zone `auth` 10/min, zone `heavy` 6/min, zone
`general` 600/min, all keyed on the TCP peer). The api layer adds what
Caddy cannot: enforcement shared across api replicas (one Redis),
per-USER granularity (Caddy has no JWT), and coverage for traffic that
never crosses Caddy (compose-network peers; the dev box's :8000).

### Buckets

| Bucket | Route(s) | Key | Default | Env var |
|--------|----------|-----|---------|---------|
| `signup_ip` | `POST /auth/signup` | client IP | `5/60` | `SERMON_API_RATELIMIT_SIGNUP_IP` |
| `login_ip` | `POST /auth/login` | client IP | `10/60` | `SERMON_API_RATELIMIT_LOGIN_IP` |
| `summary_user` | `POST /search-summary` | JWT `user_id` | `5/60` | `SERMON_API_RATELIMIT_SUMMARY_USER` |

Format is `"<max requests>/<window seconds>"` (fixed window); malformed
values fail at boot (`settings.parse_rate` validator), and limits are
read at request time so env/monkeypatch changes take effect live.

**Adding or widening a bucket** (e.g. Phase 36's generous
documents-autosave bucket, ~1 PATCH/2 s sustained → something like
`60/60`) is three one-liners: a `ratelimit_<name>` field on
`ApiSettings` (+ the field name in its validator list), a
`"<name>": lambda: settings.ratelimit_<name>` entry in
`ratelimit._BUCKETS`, and a route dependency —
`dependencies=[Depends(ratelimit.ip_limit("<name>"))]` for per-IP, or a
tiny module-local dependency on `CurrentUserDep` that calls
`ratelimit.enforce("<name>", str(current_user.user_id))` for per-user
(see `summary.py:_summary_rate_limit`; defined route-side so
`ratelimit.py` never imports `auth`). Record the new row in this table.

### Keying — IP vs user (load-bearing)

- **Public routes key on the client IP** via `ratelimit.client_ip`: the
  TCP peer by default; the first `X-Forwarded-For` entry ONLY when
  `SERMON_API_TRUST_PROXY_HEADERS=true`. In prod every browser reaches
  the api through Caddy → web, so the peer is always the web container —
  the web auth proxies therefore forward the Caddy-attested XFF
  (`web/lib/http.ts:clientIpHeaders`; Caddy discards client-supplied
  XFF, Caddyfile), and the prod compose enables trust. NEVER enable
  trust where clients can reach :8000 directly (dev default is off —
  fail closed; a spoofed XFF is then ignored entirely). uvicorn runs
  with `--no-proxy-headers` everywhere (`Makefile` dev target +
  `Dockerfile` CMD, keep in lockstep): its default `proxy_headers=on`
  rewrites `request.client` from XFF for loopback peers — a hidden
  second trust knob that live-verify caught spoofing the dev buckets.
  `SERMON_API_TRUST_PROXY_HEADERS` is the ONLY XFF trust decision.
- **Authed expensive routes key on `current_user.user_id`, never IP** —
  behind the web proxy all users share one source IP, so per-IP would
  let one user exhaust everyone. The per-user dependency sits in the
  route decorator so the 429 fires BEFORE retrieval and the paid LLM
  call (FastAPI solves decorator dependencies first).

### Mechanics & posture

- Counters live in the broker Redis, **logical db 2** (`LIMITER_DB`).
  db 0 = Celery broker, db 1 = result backend. Deliberate decision:
  `tasks_client.RedisSettings` (the lockstep mirror of
  `worker/celery_app.py`) is NOT extended with a limiter field — db 2 is
  an api-only concern, so `ratelimit.py` calls `RedisSettings().url(2)`
  and the mirror stays byte-identical to the worker's.
- **429 contract**: FastAPI-shaped `{"detail": ...}` + `Retry-After`
  header (seconds left in the window), matching Caddy's edge 429s — the
  web proxies pass status + detail to the browser verbatim, so the body
  must never name Redis, the bucket, or the key (the Redis URL embeds a
  password; same no-leak pinning as `/readyz`).
- **Fail OPEN on Redis outage**, with a loud `WARNING` log. This edge is
  abuse mitigation, not authorization: Redis-down already breaks /upload
  and flips /readyz, and failing closed would turn a store blip into a
  sitewide login lockout. `SERMON_API_RATELIMIT_ENABLED=false` is the
  operational kill switch (e.g. false-positive lockouts mid-incident).
- `/healthz` and `/readyz` are deliberately unlimited — compose
  HEALTHCHECK (every 15 s) and the future k8s probes poll them.
- Tests: monkeypatch the `ratelimit._hit` seam (the `readyz._probe_*`
  convention) — never require a live Redis.

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
you change one, change both. The Phase 19 rate limiter reuses this
module's `RedisSettings().url(2)` for its counters (logical db 2) but
deliberately does NOT add a field to the mirrored class — db 2 is an
api-only concern and the mirror must stay byte-identical to the
worker's (see "Rate limiting" above).

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
