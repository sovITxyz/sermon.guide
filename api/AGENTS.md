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
  `collections` / `reading_positions` MUST filter by `user_id`** derived
  from the JWT. For `reading_positions` that includes JOINs: the
  `/library` progress join is ON (user_id AND book_id) — `book_id` alone
  leaks another tenant's position for a shared deduped book (Phase 32).
- **`chunks` has no `user_id` BY DESIGN** — the tenant gate for reading
  a book's text is `user_library` membership, resolved per request
  (`reader._membership_stmt`) BEFORE any chunk query runs. Non-owned,
  nonexistent, and non-UUID `book_id`s are the same 404 (the
  cross-tenant-404 rule below).

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
  TCP peer by default; the **rightmost** `X-Forwarded-For` entry ONLY
  when `SERMON_API_TRUST_PROXY_HEADERS=true`. Rightmost, never leftmost:
  the rightmost hop is written by our own proxy and is the only part of
  the list a client cannot forge — correct whether Caddy REPLACES a
  client-supplied XFF (modern ≥2.5 default, no trusted_proxies) or some
  hop ever APPENDS to it; leftmost parsing would let an attacker rotate
  the bucket per request (tenant-audit finding, fixed in-phase). In prod
  every browser reaches the api through Caddy → web, so the peer is
  always the web container — the web auth proxies forward Caddy's XFF
  verbatim and add no hop (`web/lib/http.ts:clientIpHeaders`), and the
  prod compose enables trust. Revisit the hop choice only if a
  CDN/multi-hop chain lands in front of Caddy. NEVER enable
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

`api/` reaches into `../worker` for six things only — `db` (since
Phase 7), `embedding.embed`, `inference` (the Phase 16b remote
transport: `embed_texts`, `rerank_scores`, and the exception taxonomy
`main.py` maps to 503/502), `scripts.bootstrap_milvus`'s
`COLLECTION_NAME` + `make_client`, `retrieval` (the hybrid
dense+sparse+RRF kernel from Phase 12), and — since Phase 43 —
`convert` (the DOCX round-trip: `convert_to_docx`, `convert_from_docx`,
`ConversionError`, used by `documents.py`'s export/import routes). The
api venv accordingly carries `pymilvus`, `numpy`, `openai`, `httpx`,
`psycopg` (the sync driver `embedding.py`'s space guard reads its meta
row with), and `pypandoc` (`worker.convert` imports it at module scope;
pinned in lockstep with `worker/pyproject.toml`). Keep `pymilvus` and
`pypandoc` pinned in lockstep with `worker/pyproject.toml` — drift
surfaces as a wire-protocol / behavior mismatch only at runtime; the
model-weight lockstep concern died with Phase 16b (no process loads
weights).

`worker.convert` itself imports `pypandoc` DIRECTLY and shells out to
the bundled Node CLI (`worker/convert_node/`); it MUST NOT import
`worker.extractors` / `ingest` / `chunking` / `dedup` / `celery_app` /
`tasks.*` — it stays out of the ingestion graph (the same ban below
applies transitively through it). The `pandoc` binary, Node 22, and the
populated `worker/convert_node/node_modules` are **new api+worker image
deps for Phase 29 to bake** (Dockerfiles untouched this phase; the dev
box already has all three). A missing host dep surfaces as a
`ConversionError` from `convert.py`, which the routes map to a
fixed-detail 502 — never a stack-trace oracle.

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
endpoint — DeepInfra's compat endpoint by default (operator decision
2026-06-12, amending ADR 0005's original google default: the summary
LLM rides the same `DEEPINFRA_API_KEY` as embeddings/rerank/highlight,
one vendor + one key), Google via `SERMON_API_LLM_PROVIDER=google`,
ppq.ai via `SERMON_API_LLM_PROVIDER=ppq`, with `summary.py:_PROVIDERS`
as the single provider map and the unprefixed `DEEPINFRA_API_KEY` /
`GOOGLE_API_KEY` / `PPQ_API_KEY` as keys.
`SERMON_API_LLM_REASONING_EFFORT=none` (Phase 16b) disables Gemini 2.5
Flash thinking on providers that honor it.

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
"tasks.ingest.ingest_book", args=[…], task_id=…)`. Drift between this
module's `RedisSettings` and `worker/celery_app.py:RedisSettings` is a
silent failure — the api enqueues into a queue the worker isn't
reading. If you change one, change both. The Phase 19 rate limiter
reuses this module's `RedisSettings().url(2)` for its counters (logical
db 2) but deliberately does NOT add a field to the mirrored class — db
2 is an api-only concern and the mirror must stay byte-identical to the
worker's (see "Rate limiting" above). Since Phase 20 the `task_id` is
REQUIRED and minted by the `/upload` route, never by Celery — the
`upload_tasks` row must exist under that id before the broker sees the
message (see "Upload integrity" below).

## Upload integrity (Phase 20)

`POST /upload` + `GET /tasks/{task_id}` form one contract, backed by
the `upload_tasks(task_id, user_id, book_id, filename, created_at)`
table (`worker/db/models.py`, migration 0004):

- **Task ownership, 404 contract.** `GET /tasks/{task_id}` resolves the
  row scoped to the JWT user (`uploads._ownership_stmt` — BOTH
  predicates load-bearing, compile-pinned in `tests/test_uploads_unit.py`).
  Non-owned, nonexistent, and non-UUID ids are the SAME 404 — no
  existence oracle (the cross-tenant-404 rule above). The Celery backend
  is consulted only AFTER ownership passes: `AsyncResult` reports
  `PENDING` for ids it has never seen, so backend-first would make the
  route a universal 200 prober. This replaces the Phase 10 "122-bit
  task_id is the capability" posture.
- **Commit-before-send ordering.** The route commits the `upload_tasks`
  row, THEN calls `send_task` with the api-minted task UUID. A crash
  between the two leaves an owned row whose task never runs (polls as
  `PENDING`; the user re-uploads). The reverse order could run a task
  its owner can never see — and whose worker-side idempotency claim row
  is missing. Keep the ordering; it is asserted in the route tests.
- **Idempotency claim (the Phase 9 orphan-vector window).** The same row
  carries the worker's in-flight `book_id` claim: a redelivered task
  scrubs the crashed attempt's partial vectors and re-runs under the
  same `book_id` — one consistent record, zero orphans. Worker-side
  design + invariant live in `worker/ingest.py` ("Task-id claim") and
  `worker/AGENTS.md` ("Idempotency — the task-id claim").
- **Result durability caveat.** `result_expires=3600` (worker config):
  an owned task's Celery result vanishes from Redis after 1h and the
  status reverts to `PENDING`. The `upload_tasks` row keeps ownership +
  the 404 contract correct forever; persisting the *outcome* to the row
  is deliberately deferred until a product surface needs it.

## Documents — sermon storage (`documents.py`, Phase 34)

`documents.py` is the storage + API half of the B2 sermon editor (slice
A); Phases 35-37 build the web side on this surface (no web in Phase 34).
Canonical sermon storage is TipTap/ProseMirror JSON in `documents.content`
JSONB (Cross-item contract; markdown-canonical was rejected). The table is
user-owned like `highlights` — every query filters by the JWT
`user_id`, a non-owned `document_id` is a uniform 404 with no existence
oracle (the cross-tenant-404 rule above). Migration 0006 (`worker/db`).

- **`content_text` is ALWAYS server-derived, NEVER client-supplied.**
  `documents.derive_content_text` is a pure helper that walks the
  ProseMirror JSON node tree, concatenating every `text`-node's text and
  joining block-level nodes with a newline; non-text leaves (image, hard
  break, a citation node with no text) contribute nothing; malformed input
  degrades to `""`. It is re-derived on EVERY create + content-PATCH. The
  field is forbidden on the request models (`extra="forbid"`) so a smuggled
  value — which could disagree with `content` — is a hard 422, not a
  silently-dropped key. The list preview = the first `PREVIEW_CHARS`
  (**280**) chars of `content_text`; the list NEVER ships the full
  `content` JSON (that is the GET-full endpoint's job).
- **The ~2 MB cap is measured on the SERIALIZED `content` JSON byte size**
  (`MAX_CONTENT_BYTES = 2 * 1024 * 1024`), enforced in-handler on both
  create and content-PATCH → 413 (the `uploads.py` 413 shape;
  `HTTP_413_REQUEST_ENTITY_TOO_LARGE` to match that module). Measured with
  `json.dumps(..., ensure_ascii=False)` so multibyte text counts its real
  UTF-8 length. Forward note: this ~2 MB cap is enforced IN-HANDLER, after
  Starlette has already buffered the whole request body into memory; a
  pre-deserialize / global ASGI body-size limit is a future cross-cutting
  hardening (not Phase 34 scope). The node-tree walk itself is iterative
  (`derive_content_text`), so a small-but-deeply-nested payload under the
  cap cannot `RecursionError`-500.
- **`schema_version` is server-managed** — the `SCHEMA_VERSION = 1` module
  constant is the authoritative source; the DB column DEFAULT is only a
  backstop. Never accepted from the body.
- **PATCH is partial + optimistically concurrent.** Body carries
  `base_updated_at` (REQUIRED), and `title` and/or `content` (at least one
  — an empty patch is a 422). A `base_updated_at` that doesn't equal the
  stored `updated_at` is a **409** (single-author concurrency, no versions
  table — B2). The gate SELECT (`_owned_active_stmt`) runs first (ownership
  + active + 404-no-oracle + carries the prior `updated_at` for the 409),
  then a Core `UPDATE … RETURNING` (`_update_stmt`) applies the change and
  **bumps `updated_at` EXPLICITLY via `func.now()`** in the value set — the
  column has `server_default` but NO `onupdate` (schema-wide convention),
  so without the explicit bump the next PATCH's gate value would never
  move. (Same explicit-bump rationale as `reader._position_upsert_stmt`.)
- **Soft delete + restore.** `DELETE` sets `deleted_at` via a scoped
  `UPDATE … RETURNING` on an ACTIVE row (`_delete_stmt`); a soft-deleted
  doc reads as 404 on GET/PATCH/DELETE (the active gate excludes
  `deleted_at IS NOT NULL`), so a **double-DELETE is a 404**, symmetric
  with GET-on-deleted. `POST /documents/{document_id}/restore` clears
  `deleted_at` — it is the ONLY endpoint that resolves through
  `_owned_any_stmt` (no `deleted_at IS NULL` predicate) so it can SEE
  soft-deleted rows, but it KEEPS the `user_id` gate (a cross-tenant
  restore is the same 404). Restoring an already-active doc is an
  idempotent no-op 200.
- **Statement builders are the tenant seam.** Every query is factored into
  a module-level `_xxx_stmt` (`_list_stmt`, `_owned_active_stmt`,
  `_owned_any_stmt`, `_update_stmt`, `_delete_stmt`) so its `user_id`
  predicate is compile-pinned in `tests/test_documents_unit.py` (the
  `library._library_stmt` pattern) — drop the predicate and every user sees
  every user's sermons. The `(user_id, updated_at DESC)` index
  (`ix_documents_user_updated`) backs the list's `ORDER BY updated_at DESC`.
- **No rate-limit bucket in Phase 34.** The documents-autosave bucket
  (~60/60) is web/autosave-driven and deferred to Phase 36 (see "Rate
  limiting" → "Adding or widening a bucket"); new routes get no limiter
  automatically.

### DOCX round-trip (`documents.py`, Phase 43)

Two endpoints mount under the EXISTING `documents` resource (the api has no
`/sermons` prefix — the product term "sermons" == the documents resource; the
web `/sermons` editor proxies to `/api/documents/...`. Naming deviation
recorded here). Both go through `worker.convert` (the 6th cross-package import;
see "Cross-package imports from `worker/`"), which shells out to pandoc + the
Node leg. A `ConversionError` (a missing/failed host dep) maps to a
**fixed-detail 502** — never a stack-trace oracle.

- **`GET /documents/{document_id}/export.docx`.** `_require_owned_document`
  gate FIRST (same byte-identical 404 no-oracle as GET-full), then
  `convert_to_docx(doc.content)` → a `Response` with the docx `Content-Type`
  (`application/vnd.openxmlformats-officedocument.wordprocessingml.document`)
  and a `Content-Disposition: attachment; filename="<sanitized-title>.docx"`.
  The title is user-controlled, so the filename is sanitized through
  `_export_filename` (the `uploads._sanitize_filename` class — no
  header-injection / path chars; empty → `sermon.docx`).
- **`POST /documents/{document_id}/import`** (multipart `file`). The
  attacker-controlled-upload pipeline, in order: (1) read the body capped at
  `MAX_CONTENT_BYTES` → **413** the moment it crosses, then libmagic-**sniff
  the bytes** (`_sniff_docx`) → **415** on non-docx — BOTH before any disk
  write or pandoc run (the `uploads.py` edge-sniff posture; content bytes, not
  the `Content-Type` header); (2) stage under `settings.upload_dir` in a
  per-import UUID subdir with a `finally` that ALWAYS deletes the staged file
  AND removes the subdir; (3) `_require_owned_document` gate (after the cheap
  edge checks); (4) `convert_from_docx` (→ 502 on failure); (5) re-cap the
  converted JSON to `MAX_CONTENT_BYTES` and **RE-DERIVE `content_text`** via
  `derive_content_text` (NEVER trust the conversion output); (6)
  **snapshot-FIRST** in ONE transaction — INSERT the CURRENT (pre-overwrite)
  `content`/`content_text`/`user_id`/`schema_version` into
  `sermon_doc_revisions` (`_revision_insert_stmt`), THEN `_update_stmt`
  overwrites `documents.content`/`content_text` + bumps `updated_at`, THEN
  commit. The snapshot predates the overwrite, so an import is never
  destructive.
- **Tenant gate.** Both endpoints scope by the JWT `user_id` via
  `_require_owned_document`; the snapshot row's `user_id` is the JWT user (the
  denormalized tenant gate, `worker/db` migration 0008). Nothing reads a
  `user_id`/`document_id` from the body. `_revision_insert_stmt` is a
  module-level `_xxx_stmt` builder so its tenant column is compile-pinned in
  `tests/test_documents_unit.py` like the rest.
- **Security.** Import is the only attacker-controlled docx surface: size-cap +
  byte-sniff before disk, pandoc runs with no network, the staged `/tmp` file
  is always cleaned (`finally`), the request takes only the multipart file
  (no JSON body to smuggle fields through), and the imported document renders
  as TipTap JSON (zero `dangerouslySetInnerHTML`). Any `<a href>` the Node leg
  reconstructs into a citation is validated to the `/read/{bookId}?chunk=...`
  shape (the `worker/convert_node` citation extension rejects
  `javascript:`/`data:`/external/protocol-relative hrefs; StarterKit's Link is
  disabled so non-citation links degrade to text).
- **No rate-limit bucket in Phase 43** (like documents/calendar).

## Calendar — sermon events (`calendar_routes.py`, Phase 38)

`calendar_routes.py` is the server half of the B3 preaching calendar
(Phases 39-42 are pure web on top of this API). `sermon_events` is
user-owned like `documents` — every query filters by the JWT `user_id`, a
non-owned `event_id` is a uniform 404 with no existence oracle (the
cross-tenant-404 rule above). Migration 0007 (`worker/db`).

- **The module is `calendar_routes.py`, NOT `calendar.py`.** A file literally
  named `calendar.py` is shadowed by the stdlib `calendar` under pytest's
  `pythonpath=["."]` — `from calendar import …` resolves to
  `/usr/lib/python3.12/calendar.py` (ImportError at collection AND a pyright
  `reportAttributeAccessIssue`). The router file is `calendar_routes.py`;
  `main.py` does `import calendar_routes` (plain, no alias) and the route
  prefix is still `/calendar`. If you add another stdlib-named module, do the
  same.
- **`event_date` is a DATE, not a timestamptz** (the schema's first DATE
  column). Preaching is day-anchored; a UTC-midnight timestamptz shifts a day
  for UTC-minus users. The GET `start`/`end` query params are DATE too, and
  dates stay `YYYY-MM-DD` end-to-end into web/.
- **GET range is half-open `[start, end)`, capped at `RANGE_CAP_DAYS`
  (400).** An event dated exactly `end` is EXCLUDED (the `_range_stmt`
  `event_date < end`, NOT `<=`, is load-bearing and compile-pinned). `start`
  must be `<= end` (else 422) and the span `end - start` must be `<= 400`
  days (else 422 — a full year-view is one call; the resolution of the B3
  "GET range cap value" open question). Results are `event_date`-ordered.
- **POST materializes DISCRETE weekly rows, capped at
  `MATERIALIZER_CAP_ROWS` (53).** Body `{event_date, title, series?,
  document_id?, repeat_weekly_until?}` (`extra="forbid"`). A non-null
  `repeat_weekly_until` writes one row per 7-day step from `event_date`
  through it inclusive — `repeat_weekly_until >= event_date` (else 422) and
  the row count `<= 53` (else 422; 52 weeks + 1). Each materialized row is an
  INDEPENDENT `sermon_events` row (no parent linkage), so each PATCHes /
  DELETEs on its own. The default (no `repeat_weekly_until`) is one row. POST
  returns every created event.
- **`document_id` is ATTACKER-CONTROLLED body input — ownership-check it.**
  The nullable FK alone does NOT scope tenancy. On POST/PATCH, a non-null
  `document_id` MUST resolve to a document the JWT user owns
  (`_document_owned_stmt` — `Document.document_id == id AND Document.user_id
  == JWT`); a miss raises the SAME 404 used for a nonexistent event, byte-
  identical whether the doc is another tenant's or nonexistent (no existence/
  title oracle — without this, user B could link user A's document and the
  calendar would leak its existence). **The ownership check has NO
  `deleted_at IS NULL` predicate** — a soft-deleted but owned doc is
  acceptable (ownership is what matters), unlike the documents GET gate. The
  gate runs BEFORE any write.
- **PATCH is partial; `document_id` is three-state.** Body
  (`extra="forbid"`) carries any subset of `event_date`/`title`/`series`/
  `document_id`; at least one field must be present (an empty patch is a 422,
  checked via `model_fields_set`). `document_id` distinguishes ABSENT (leave
  the link alone), present-and-`null` (DETACH), and present-and-non-null
  (re-link under the SAME ownership check) — also via `model_fields_set`, so
  a `null` is not mistaken for "unset". There is NO optimistic-concurrency
  `base_updated_at` gate here (that is documents-specific — single-author
  manuscript edits; calendar events have no such requirement); PATCH is a
  plain doubly-scoped partial update that bumps `updated_at` EXPLICITLY via
  `func.now()` (no `onupdate` on the column — schema-wide convention).
- **DELETE is a HARD delete** (`_delete_stmt` — a real `DELETE … RETURNING`,
  doubly-scoped), NOT a soft delete like documents: calendar events are cheap
  and regenerable, so there is no `deleted_at`/restore surface. A non-owned /
  nonexistent / non-UUID id is the same 404.
- **Statement builders are the tenant seam.** Every query is a module-level
  `_xxx_stmt` (`_range_stmt`, `_owned_event_stmt`, `_update_stmt`,
  `_delete_stmt`, `_document_owned_stmt`) so its `user_id` predicate (and the
  half-open range bounds) are compile-pinned in `tests/test_calendar_unit.py`
  (the `library._library_stmt` pattern). Every per-id builder is DOUBLE-scoped
  (`event_id` AND `user_id`). `ix_sermon_events_user_date (user_id,
  event_date)` backs the range scan.
- **No rate-limit bucket in Phase 38.** Like documents, calendar CRUD gets no
  limiter automatically; add one (per "Rate limiting") only if a web surface
  drives abusive volume.

## Integrations — OAuth token vault (`integrations.py` + `crypto_vault.py`, Phase 44)

The B4 OAuth vault. A user connects their Google account so a later phase
(45) can pull/push sermons to Drive; this phase mints + stores the encrypted
refresh token and surfaces only the connection's identity (email).
`oauth_connections` is user-owned (migration 0009, `worker/db`); every query
filters by the JWT `user_id`, a cross-tenant / never-connected provider is a
byte-identical 404. NO google SDK — two thin `httpx` calls (token exchange +
userinfo). `POST /integrations/{provider}/authorize`, `GET
/integrations/{provider}/callback`, `GET /integrations`, `DELETE
/integrations/{provider}`.

- **Tokens are NEVER stored in plaintext.** `crypto_vault.encrypt(str)->bytes`
  / `decrypt(bytes)->str` is AES-256-GCM (`SERMON_API_TOKEN_ENC_KEY`, 64 hex =
  32 bytes). Layout is `nonce(12 random bytes) || ciphertext+tag` — a fresh
  random 96-bit nonce per call (the GCM invariant; never reuse a nonce with a
  key). A tampered/truncated blob raises `InvalidTag` (the route lets it
  surface as a 500 — never a detail oracle). The DB holds `BYTEA` ciphertext
  only; the ONLY token-derived value ever returned to the browser is
  `provider_account_email`. The list endpoint selects NO ciphertext column.
- **Validate-on-use, not at boot.** Empty/malformed Google client id/secret or
  vault key raises `crypto_vault.OAuthUnconfiguredError` -> **503** naming the
  env var (the `MissingInferenceKeyError` -> 503 posture; mapped in
  `main.py`). The app STILL BOOTS with Google unconfigured — none of the new
  settings arm a boot guard.
- **`state` is account-bound, HMAC-signed, expiring.** `state =
  b64url(payload) + '.' + b64url(HMAC-SHA256(payload))`, payload `{user_id,
  nonce, provider, exp}`, key `SERMON_API_OAUTH_STATE_SECRET` (falls back to
  `jwt_secret`). The HMAC key decouples OAuth-state forgery from session JWTs.
- **THE phase deliverable: the callback validates EVERYTHING before the token
  exchange.** Strict order in `callback`, all BEFORE the httpx token POST:
  (a) HMAC constant-time compare, (b) `exp` not past, (c) `provider` matches
  the path, (d) **`state.user_id == current_user.user_id`** — the
  account-binding CSRF defense (without it an attacker binds a victim's
  session to the attacker's Google account), (e) atomic GETDEL of the
  single-use PKCE verifier from Redis. Any failure is a generic 400 (no
  oracle). The compile-pin test mocks httpx and asserts it is NOT called on a
  bad-state / missing-verifier request.
- **PKCE verifier lives in Redis (db 2), keyed by the state nonce — NOT a
  cookie.** The web `/api/integrations/{provider}/callback` route forwards the
  user's bearer to this api callback; the web->api hop does not carry the
  browser cookie to the api origin, so a web-origin cookie is unreadable here.
  Redis-keyed-by-nonce is the cross-hop store with free TTL (`oauth:pkce:` ==
  the state lifetime, ~10 min). `SET` at authorize, `GETDEL` (single-use) at
  callback — a second redeem fails.
- **`access_type=offline` + `prompt=consent` are REQUIRED** to receive a
  refresh token, and a FRESH one on every reconnect; the callback rejects a
  token response with no refresh token. The UPSERT is ON CONFLICT(user_id,
  provider) DO UPDATE — reconnect overwrites in place (`uq_oauth_connections_
  user_provider`), bumping `updated_at` EXPLICITLY via `func.now()` (no
  `onupdate` — schema-wide convention).
- **`redirect_uri` is derived from ONE settings source** (`settings.web_origin`
  + `/api/integrations/{provider}/callback`) so authorize and the token
  exchange use a byte-identical value; any drift is `redirect_uri_mismatch`
  from Google. It is the WEB origin (operator-registered, ports 3000/3001),
  not the api origin.
- **DELETE is a hard delete** scoped to (user_id, provider), with a best-effort
  POST to Google's revoke endpoint using the decrypted refresh token (failure
  swallowed/logged WITHOUT the token — the local delete is authoritative). A
  cross-tenant / never-connected / unknown provider is the same 404.
- **NEVER log the `code`, `code_verifier`, `code_challenge`, refresh/access
  tokens, `client_secret`, or ciphertext.** `code` is too generic to add to
  the global deny-list (it would scrub `status_code`), so the discipline of
  never passing the value to a log call is the primary defense — log only
  `provider`, `user_id`, outcome. `refresh_token`/`access_token`/
  `code_verifier`/`client_secret` ARE explicit deny-list entries
  (`observability.py` + `worker/obs.py`, kept in lockstep).
- **Statement builders are the tenant seam** (`_list_stmt`, `_connection_stmt`,
  `_upsert_stmt`, `_delete_stmt`) — `user_id` compile-pinned in
  `tests/test_integrations_unit.py` (the `library._library_stmt` pattern).
- **`microsoft` is Phase 46** — the `{provider}` path param + the
  `_ALLOWED_PROVIDERS` allow-set stay generic; an unconfigured/unknown provider
  is a 404 (never a 500), so adding `microsoft` is config-only.

## Content-type posture (Phase 20 — early sniff, decided)

`POST /upload` libmagic-sniffs the FIRST BYTES of the body and 415s
anything that isn't `application/epub+zip` / `application/pdf` —
*before* any disk write, DB row, or enqueue. This reverses the Phase 10
"no format trust at the API" stance deliberately: that rationale argued
against trusting the client's Content-Type *header*; the sniff inspects
content bytes — the same evidence the worker sees — so there is no
header for an attacker to vary. What it buys: attacker bytes are never
staged to `upload_dir`, no ownership row or queue slot is burned on a
guaranteed-failure task, and the uploader gets an immediate 415 instead
of polling to `FAILURE`. The worker's `extractors.detect()` still
re-sniffs the staged file and remains authoritative (the api sees only
the stream head). `uploads._ALLOWED_UPLOAD_MIMES` is a MIRROR of
`worker/extractors/extract.py:_MIME_TO_FORMAT` — mirrored, NOT imported
(the `worker.extractors` import ban below); change both sides in the
same PR, same rule as the `_sanitize_filename` mirror. Runtime needs
the `libmagic` system library (`api/Dockerfile` installs `libmagic1`;
GitHub's ubuntu runners ship it).

## Observability — logging, metrics, Sentry (`observability.py` + `metrics.py`, Phase 27)

Three deps, all py.typed (the minio-py rationale): `structlog` (JSON logs +
contextvars correlation), `prometheus-client` (`/metrics`), `sentry-sdk[fastapi]`
(env-gated error reporting). The worker mirrors `structlog` + `sentry-sdk[celery]`
but carries NO `prometheus-client` (no pushgateway, short-lived prefork → not
scrapable; it emits ingest timings as correlated JSON logs instead).

- **Structured JSON logging.** `observability.configure_logging()` (called at
  `main.py` import, idempotent) wires `structlog` as a
  `ProcessorFormatter` on the stdlib ROOT handler with a `foreign_pre_chain`,
  so EVERY existing `logging.getLogger(__name__).warning(..., exc_info=...)`
  call (search/readyz/ratelimit/…) renders as one-line JSON AND gets redacted
  — no call-site change. A structlog-only config would let those lines bypass
  the deny-list (a leak); the stdlib-bridge test pins it. `ExtraAdder` runs
  BEFORE `redact_event` in the pre-chain so an `extra={"dsn": ...}` is scrubbed.
- **Correlation id.** `CorrelationMiddleware` (pure-ASGI, added OUTERMOST in
  `main.py` so even CORS-rejected/4xx responses get an id) reads inbound
  `X-Correlation-ID` (or mints `uuid4().hex` when absent/garbage), binds it via
  `structlog.contextvars` so every log line on the request carries it, echoes it
  on the response, times the request into `REQUEST_DURATION`, and clears
  contextvars in a `finally`. It NEVER logs the request body or headers — only
  the correlation header by name. It propagates into Celery via
  `tasks_client.enqueue_ingest` (`send_task(headers={CELERY_CORRELATION_KEY: ...})`
  — task signature unchanged); the worker's `task_prerun` rebinds it.
- **Redaction deny-list.** `observability.redact_event` (a structlog processor
  reused as Sentry `before_send`) replaces any value whose KEY contains a
  deny-listed substring (`authorization`, `token`, `password`, `secret`,
  `api_key`/`apikey`, `dsn`, `jwt`, `cookie`, …) with `[REDACTED]`. HARD RULES
  (key-substring matching is belt-and-suspenders, not the primary defense): (1)
  request bodies are NEVER logged; (2) request headers are never dumped
  wholesale; (3) JWT claims / the bearer token never enter a log call; (4)
  Redis/Postgres URLs (password-bearing) are never interpolated into a message;
  (5) Sentry `send_default_pii=False`. A reviewer MUST verify no new log call
  interpolates a secret into the MESSAGE text (key matching can't catch that).
- **`/metrics` (public + unlimited).** Same posture as `/healthz`//`/readyz` —
  no auth, no rate-limit dependency. `prometheus_client.generate_latest` over
  the default `REGISTRY`. Collectors (declared ONCE at `metrics.py` scope —
  re-import must not `Duplicated timeseries`): `REQUEST_DURATION`
  Histogram{route,method,status} (route = matched APIRoute path TEMPLATE, never
  the raw path — a UUID-per-label would explode Prometheus memory),
  `RETRIEVAL_STAGE` Histogram{stage} (embed/dense/sparse/rerank/highlight/llm,
  timed at the seams in `search.py`/`summary.py`), `RETRIEVAL_DEGRADED`
  Counter{stage} (incremented at each `run_search` degraded site — the Phase 22
  trust-gap tell: non-zero under healthy deps = an in-our-code bug), and
  `CELERY_QUEUE_DEPTH` Gauge{queue} (refreshed on scrape via Redis `LLEN` on the
  BROKER db 0, fail-soft like `/readyz` — never 500 the scrape). The gauge is a
  backlog APPROXIMATION: `LLEN` undercounts in-flight `acks_late` messages and
  ignores non-default queues (documented on the metric).
- **Sentry.** OFF BY DEFAULT in dev: `init_sentry()` is a total no-op (zero
  network) when `SERMON_API_SENTRY_DSN` is unset/empty (the empty-string-is-None
  validator means compose's `${VAR:-}` keeps it off). When set, init runs in the
  lifespan with `FastApiIntegration`, `send_default_pii=False`, `traces_sample_rate`
  default 0, `environment=settings.env`, and the `before_send` scrubber.
- **Mirrored, not imported (dep-direction rule).** `CELERY_CORRELATION_KEY ==
  "correlation_id"` and the redaction deny-list each have ONE copy in
  `api/observability.py` and ONE in `worker/obs.py`, each doc-commented as the
  other's lockstep mirror (the `tasks_client.RedisSettings` /
  `uploads._ALLOWED_UPLOAD_MIMES` precedent). `worker/obs.py` MUST NOT import
  from `api/`; `api/observability.py` MUST NOT import `worker.celery_app` or
  `worker.tasks.*`. Propagation rides Celery MESSAGE HEADERS, so no import
  boundary or task signature changes. If you change the header/key string or the
  deny-list on one side, change BOTH — `test_observability_unit.py` asserts the
  key equality to catch drift. Note: `metrics.py` imports `tasks_client.RedisSettings`
  LAZILY (inside `_refresh_queue_depth`) to break the `tasks_client → observability
  → metrics → tasks_client` import cycle.

## Graceful degradation (Phase 22)

A single dependency blip must not 500 the retrieval path. The contract
lives in `search.run_search` (mechanics in the `search.py` module
docstring; the summary posture in `summary.py`'s "Degraded retrieval"
section):

- **Dense/sparse fan-out** runs with `return_exceptions=True`. One arm
  down → the surviving arm's results + the failed arm's name in the
  response's `degraded` list. Both arms down → **503** with a fixed
  detail (retryable dependency outage, not a bug → not 500; internal
  infra, not a gateway → not 502; detail never carries the exception —
  the `/readyz` never-body-the-failure rule).
- **Milvus-down is budget-bounded, not a 10 s+ long-tail**: `make_client`
  sets a client-level connect timeout and `dense_search` passes a per-RPC
  deadline (both `MILVUS_TIMEOUT_SECONDS = 2.5` in
  `worker/scripts/bootstrap_milvus.py`), but pymilvus 2.6 runs a
  hardcoded-10 s in-request reconnect BEFORE honoring that deadline on a
  warm connection's first failure. `search.py` therefore (1) wraps the
  dense arm's whole Milvus leg (client checkout + search RPC) in
  `asyncio.wait_for` under `DENSE_ARM_BUDGET_SECONDS = 4.0` — budget
  expiry degrades the response while the worker thread is orphaned (it
  drains within pymilvus's own 10 s ceiling); and (2) resets the
  process-wide client singleton on any dense-arm `MilvusException` so the
  next request reconstructs it — without the reset, pymilvus's
  post-recovery closed channel raises non-gRPC errors its recovery never
  retries, leaving the dense arm dead past the outage. Residual caveat:
  pymilvus health-checks its cached connection entry only after a 30 s
  idle gap, so under sustained sub-30 s traffic the dense arm stays
  (bounded-fast) degraded until construction attempts are ≥30 s apart.
- **Rerank, then highlight, each degrade independently**: a
  `RemoteInferenceError` (their entire realistic failure surface since
  Phase 16b; covers `MissingInferenceKeyError`) falls back to the raw
  RRF top-K, flagged `"rerank"` / `"highlight"`. A rerank failure does
  not skip highlight — it prunes the RRF-ordered fallback. Any other
  exception is a pipeline bug and still fails loud.
- **`degraded: list[str]`** rides both `SearchResponse` and
  `SummaryResponse`: stage names `dense`/`sparse`/`rerank`/`highlight`
  in pipeline order, always present, `[]` when healthy (stable +
  counter-friendly for Phase 27; additive for clients).
- **`/search-summary` proceeds with the flag, never 503s on partial
  retrieval** (decision made + documented in Phase 22, rationale in
  `summary.py`): degraded grounding is narrower, not wrong — the
  citation contract holds. A degraded-EMPTY retrieval keeps the
  no-LLM-call guard but carries the flags.
- **Degradation NEVER widens scope**: `book_ids` is resolved once from
  the JWT user's `user_library` and the same list parameterizes both
  arms; every fallback is a reshuffle/truncation of already-filtered
  in-memory hits — no retry, no re-query, no recomputed filter. Both
  arms still raise on an empty `book_id` set.
- Every degraded path logs the failure with `exc_info` (fail-loud in
  logs, soft in the response).

## Open trust gaps

- **Chunked Milvus filter, no silent cap (Phase 24).** A user with 10K
  books would produce a ~360 KB `book_id IN [...]` Milvus filter
  expression in a single search. `retrieval.dense_search` now splits the
  `book_id` set into `MILVUS_FILTER_BOOK_ID_CHUNK`-sized slices (default
  **1000**, overridable via `SERMON_MILVUS_FILTER_BOOK_ID_CHUNK`),
  keeping each per-search expr ~36 KB. Libraries `<=` the chunk size take
  the unchanged single-search fast path; larger libraries run one scoped
  search per slice (each pulling its own top-`limit`) and merge the
  per-slice hits into the global top-`limit` by COSINE distance. This
  **preserves full recall** — no book is silently dropped, which would be
  both a correctness AND a tenant-trust regression. The union of the
  per-slice filters equals exactly the input `book_id` set (contiguous,
  non-overlapping slices), so the tenant boundary holds chunk-by-chunk;
  the empty-library `ValueError` guard is unchanged. Chunked slices run
  sequentially under the same single `DENSE_ARM_BUDGET_SECONDS`
  `wait_for`, so a very large library costs proportionally more wall time
  bounded by that budget (a budget trip degrades the whole arm — never a
  partial-library result that would look like a silent cap). Phase 12's
  BM25 arm needs no chunking: `book_id = ANY(:book_ids)` binds a single
  array parameter, so a 10K-element list ships as one bound value with no
  query-text length blowup (live-gated by
  `worker/tests/test_retrieval_filter_cap.py`).
- **The retrieval arms degrade on ANY `Exception`, including our own
  bugs.** The arm fan-out's full-`Exception` breadth is deliberate (the
  dense arm's failure surface spans four libraries; enumerating them
  would couple `search.py` to transport internals), but it means an
  in-our-code `TypeError`/`KeyError` inside either arm degrades the
  response instead of 500ing — loud in the logs (`exc_info`), invisible
  in status codes. Operators watching only 5xx rates could miss a
  retrieval-arm code bug riding as permanent degradation. Phase 27's
  metrics on the `degraded` flags are the planned mitigation: a non-zero
  steady-state `degraded` counter with healthy dependencies is the tell.
- **Degraded responses lose signal quality silently beyond the flag.**
  Phase 22 closed the one-arm-down-⇒-500 gap (see "Graceful degradation"
  above), but the *semantics* of a degraded response are weaker than the
  flag alone conveys: a sparse-arm-down response loses BM25's
  corpus-presence filtering (the Phase 12 audit showed dense-only
  retrieval surfaces false positives at 0.5–0.6 COSINE for
  nothing-in-corpus queries — with no sparse signal, RRF cannot suppress
  them, and if rerank also degraded nothing else will); a rerank-degraded
  response carries RRF scores in `score` (the documented `rerank=false`
  semantics — and ADR 0006 confirms nothing thresholds the rerank score).
  Clients see only `degraded: [...]`; per-stage quality caveats in the UI
  are a later web phase. Phase 27 emits the flags as counters.
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
