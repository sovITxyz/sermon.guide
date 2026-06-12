# worker/ — agent instructions

Per-package conventions for the ingestion pipeline. See repo-root
[`AGENTS.md`](../AGENTS.md) for cross-package rules and
[`ARCHITECTURE.md`](../ARCHITECTURE.md) for system design.

## Toolchain

uv manages the venv and dependencies; `worker/.python-version` pins
Python 3.12. From `worker/`:

```bash
uv sync --all-extras --dev   # install runtime + dev deps into .venv
uv run <tool>                # run anything in the venv (ruff, pyright, pytest, python)
```

### System binaries

Two non-Python dependencies must be present on the host (CI image and dev
machines alike):

- **`pandoc`** — EPUB extraction shells out to it via `pypandoc`. Install
  with `sudo apt install pandoc` (Debian/Ubuntu) or `brew install pandoc`
  (macOS). Pinned in `pyproject.toml` as a Python wrapper but the binary
  itself is system-installed.
- **`libmagic`** — `python-magic` is a thin ctypes wrapper around libmagic;
  Ubuntu/Debian ship it via `libmagic1` (usually pre-installed via
  `file`/`util-linux`). On macOS: `brew install libmagic`.

A missing binary surfaces as `OSError`/`RuntimeError` at import or first
call — fail loudly, do not silently fall back.

Make targets (also run from `worker/`):

| target              | what it does                                                                                                       |
| ------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `lint`              | `uv run ruff check .`                                                                                              |
| `format`            | `uv run ruff format .`                                                                                             |
| `format-check`      | `uv run ruff format --check .`                                                                                     |
| `typecheck`         | `uv run pyright`                                                                                                   |
| `test`              | `uv run pytest`                                                                                                    |
| `bootstrap-milvus`  | sources `../infra/.env` and runs `scripts/bootstrap_milvus.py`. `make bootstrap-milvus ARGS=--force` drops + recreates. |
| `ingest`            | `make ingest FILE=path/to/book.epub USER=<user_uuid>` — single-book dedup-aware pipeline (Phase 8). The book_id is decided by dedup, not passed in. |
| `worker`            | `uv run celery -A celery_app worker --loglevel=info` — long-running Phase 9 Celery prefork worker. Sources `../infra/.env` for Redis broker. |
| `enqueue`           | `make enqueue FILE=path TENANT=<uuid\|label>` — test producer for `worker`. Label form auto-derives a stable uuid5 and upserts a `users` row so the FK resolves. |
| `migrate-up`        | `alembic upgrade head` against the docker-compose Postgres. Idempotent. |
| `migrate-down`      | `alembic downgrade -1` (one revision). `make migrate-down REV=base` wipes all the way. |
| `migrate-new`       | `make migrate-new MSG="describe change"` → `alembic revision --autogenerate`. Review the generated file before committing — autogenerate misses `server_default`, enum diffs, and may rename indexes cosmetically. |

## CI gates (Phase 17)

`.github/workflows/ci.yml` runs four worker-relevant jobs. Post-Phase-16b
(ADR 0006) there are no local models and no HF cache to provision in CI —
embeddings are remote DeepInfra calls keyed by `DEEPINFRA_API_KEY`.

- **worker** — lint + typecheck + `uv run pytest` with NO infra and no
  secrets. Infra-gated tests skip here BY DESIGN; never "fix" a skip by
  wiring infra into this job.
- **tenant-isolation** — boots `infra/docker-compose.yml` (`make up`,
  healthcheck `--wait`), runs `migrate-up` + `bootstrap-milvus` + the
  WordNet download, then `make -C worker test-isolation` plus the
  storage + dedup suites with `infra/.env` sourced. A no-skip guard
  FAILS the job if any of those tests report SKIPPED — the gates must
  run for real (red, not skipped). Keyless and zero spend: isolation
  vectors are synthetic (Phase 3) and the Phase 31 storage paths avoid
  remote embeddings by construction. This is the job to mark REQUIRED
  in branch protection. Adding a new infra-gated test to those suites?
  It must pass under the compose stack, or the guard will (correctly)
  go red.
- **retrieval-golden** — keyless flavor; skip-passes today (no corpus,
  no key) but the `golden-loud-skip-guard` step emits a `::warning` +
  job summary naming the activation path, so the vacuous green is
  never silent.
- **retrieval-golden-live** — activates automatically once the operator
  runs `gh secret set DEEPINFRA_API_KEY` (the filter job probes secret
  presence; no workflow edit needed). Boots compose and runs the golden
  + ingest-e2e + embedding weight-parity suites with the key wired —
  cents per run, cold ingest every run (ephemeral infra means Phase 8
  dedup idempotency never applies in CI). The golden query rows stay
  skipped until Phase 23 commits a public-domain CI corpus; infra/key
  skips in this job are treated as failures, only the corpus-gap skip
  is tolerated. Fork PRs never receive secrets, so the job skips there.

Env trap to keep in mind for any new CI step: `db/settings.py` and
`celery_app.py` defaults bake in the dev box's host-port remaps
(Postgres 54322, Redis 63792), while the compose stack created from
`infra/.env.example` listens on the standard 5432/6379. Any CI step
that touches Postgres must source the env first
(`set -a && . infra/.env && set +a`). Milvus (19530) and MinIO (9000)
defaults happen to align, which makes a missing source easy to miss.

## Banned APIs

Enforced via `[tool.ruff.lint.flake8-tidy-imports.banned-api]` in
`pyproject.toml`:

- **`datetime.datetime.utcnow`** — returns a naive `datetime` with no
  `tzinfo`, which silently miscompares with TZ-aware datetimes elsewhere in
  the stack. Use `datetime.now(tz=UTC)` (Python 3.11+).
- **`pickle`** — `pickle.load` is an arbitrary-code-execution sink: anyone
  controlling the bytes runs code as the worker process. Pickle's format
  also breaks across Python/library versions. Use `json` or `msgpack` for
  anything that crosses a trust boundary or persists.

## Extractors

`worker/extractors/` converts raw EPUB or PDF input into clean Markdown.
The contract is one function — `extract(path) -> str` — that dispatches
on `detect(path)`:

- **EPUB** (`application/epub+zip`) → `EbookLib` reads (X)HTML items in
  spine order; `pypandoc` converts the concatenated HTML to GitHub-flavored
  Markdown. This route was chosen over Apache Tika to avoid alt-text /
  metadata leakage in the output (see
  [`ARCHITECTURE.md` §2](../ARCHITECTURE.md#2-locked-decisions)).
- **PDF** (`application/pdf`) → `pymupdf4llm.to_markdown` (markdown-aware,
  preserves page structure).

Format detection MUST go through `detect()`, which sniffs MIME via
`python-magic`. **Never trust the file extension** — the ingestion pipeline
will eventually accept uploads from untrusted users, and a renamed
`malicious.epub` is the kind of thing that ends in CVEs.

CLI (from `worker/`):

```bash
uv run python -m extractors path/to/book.epub > book.md
```

The module path is `extractors` (not `worker.extractors`) because `worker/`
itself is intentionally not a package — see `pyproject.toml`'s
`package = false`. Run from `worker/` so cwd carries the `extractors/`
package onto `sys.path`. The CLI lives in `extractors/__main__.py`; the
dispatcher in `extractors/extract.py` exposes `extract()` and `detect()` as
the importable surface.

Test samples live in `worker/tests/samples/` and are **gitignored** —
copyrighted material must never be committed. Drop a small EPUB/PDF in
there to run the smoke test locally; CI skips the suite when samples are
absent.

## Chunking

`worker/chunking.py` turns extracted Markdown into semantic chunks for
embedding. `chunk(markdown) -> list[Chunk]` wraps LlamaIndex's
`SemanticSplitterNodeParser`: it embeds adjacent sentence groups and breaks
where cosine distance jumps past a percentile threshold, so boundaries fall
on shifts in meaning rather than fixed token windows
([`ARCHITECTURE.md` §2](../ARCHITECTURE.md#2-locked-decisions)).

The boundary-detection embedder is **`BAAI/bge-large-en-v1.5`** — the same
1024-d model Phase 6 uses for the chunk embeddings written to Milvus. Since
Phase 16b ([ADR 0006](../docs/adr/0006-remote-inference.md)) it is a
**remote call**: `chunking.py` wraps `inference.embed_texts` in a thin
`BaseEmbedding` adapter (`_RemoteBGEEmbedding`) so the splitter's boundary
embeddings hit the same endpoint + model id + 512-token truncation the chunk
embeddings use (`SERMON_EMBEDDINGS_*`, `DEEPINFRA_API_KEY`) — boundary
detection and chunk embedding can never disagree on weights. No model
downloads, no HF cache; the end-to-end test gates on `DEEPINFRA_API_KEY` and
skips when absent so CI doesn't fail on a 503.

`Chunk` carries `(text, start_idx, end_idx, parent_section)`. `start_idx`
and `end_idx` are character offsets into the source markdown — they are
the citation anchor downstream. `parent_section` is the nearest ATX heading
above the chunk, best-effort; `None` for chunks before the first heading.
**Invariant (Phase 21):** `parent_section` is stripped of markup at capture
via `chunking.clean_heading()` (headings that strip to empty are dropped,
falling back to the previous real heading or `None` — never `""`);
maintenance/backfill scripts MUST reuse that function, not reimplement it.

CLI (from `worker/`):

```bash
uv run python -m chunking path/to/book.md
```

The module is the single file `worker/chunking.py`; `python -m chunking`
runs it as `__main__` from the worker cwd (same pattern as `extractors`).

## Embedding

`worker/embedding.py` produces **`BAAI/bge-large-en-v1.5`** embeddings
(locked in [`ARCHITECTURE.md` §2](../ARCHITECTURE.md#2-locked-decisions))
via the remote transport in `worker/inference.py` — since Phase 16b
([ADR 0006](../docs/adr/0006-remote-inference.md)) no model weights load
in-process anywhere. `embed(texts)` keeps its Phase 6 contract: a
`(N, 1024)` float32 array with each row L2-normalized — the precondition
for Milvus' `COSINE` metric to be inner-product in disguise
([§3](../ARCHITECTURE.md#3-milvus-schema--library_vectors)). Empty input
returns a `(0, 1024)` array without touching the network, the database,
or the key.

**The transport (`worker/inference.py`)** is the shared seam both
packages use (api/ imports it for query embedding, rerank, and highlight
scoring): an OpenAI-compatible embeddings client (`openai` SDK,
`SERMON_EMBEDDINGS_BASE_URL`/`SERMON_EMBEDDINGS_MODEL`) plus a thin
`httpx` rerank client (`SERMON_RERANK_BASE_URL`/`SERMON_RERANK_MODEL` —
DeepInfra's reranker endpoint is not OpenAI-shaped), both keyed by the
unprefixed `DEEPINFRA_API_KEY`. Explicit timeouts, exactly one retry;
unset key → `MissingInferenceKeyError`, upstream failure →
`RemoteInferenceError` naming the provider + leg (api/ maps them to
503/502). Requests carry only already-authorized chunk/query text — no
`user_id`, JWT, or email ever leaves the process.

**Embedding-space guard.** A deployment's vectors live in exactly one
model's space. Migration 0003 seeds Postgres `meta('embedding_model_id')`
with the v0 model; before the first real embed of a process,
`embedding.py` compares `SERMON_EMBEDDINGS_MODEL` against that row and
**refuses to run on a mismatch** — silent provider/model drift would mix
embedding spaces and quietly destroy retrieval. Changing embedders is a
deliberate migration (re-embed the corpus, recalibrate thresholds, update
the row), never an env flip.

**Weight parity.** The point of pinning DeepInfra's `BAAI/bge-large-en-v1.5`
is that it serves the EXACT weights the in-process era used: every stored
Milvus vector stays valid and every calibrated threshold keeps its meaning.
`tests/test_embedding.py` pins this live — remote vectors must match the
committed local-model reference (`tests/golden/local_model_refvecs.npz`)
within float tolerance. If that test fails, do not loosen it; the provider
drifted and retrieval is suspect.

## Ingest

`worker/ingest.py` is the single-book dedup-aware ingest CLI as of
Phase 8. Pipeline:

```
detect → extract → MinHash signature → dedup lookup
   ├── duplicate? → insert user_library row only
   └── new?       → chunk → embed → insert vectors;
                    insert global_books + user_library; LSH.add
```

```bash
uv run python -m ingest path/to/book.epub --user-id <user_uuid>
# or via make
make ingest FILE=path/to/book.epub USER=<user_uuid>
```

`--user-id` is the JWT-derived owner; it's a FK to `users.user_id`, so
the row must already exist. The `book_id` is no longer a CLI input —
dedup decides it. New books get a fresh UUID; duplicates reuse the
existing `global_books.book_id`. `user_id` does NOT land in vector
metadata — that would defeat the dedup story per
[§7.1](../ARCHITECTURE.md#71-dedup-vs-isolation-milvus-partition-key);
vectors are shared globally per deduped book and tenant scoping happens
at the API at search time via `book_id IN (<user's library>)`.

Idempotency is property of the pipeline now: re-ingesting the same
content under the same user short-circuits at the dedup gate, then
upserts `user_library` (`ON CONFLICT DO NOTHING` on the
`uq_user_library_user_book` constraint). Under a *different* user, the
second `user_library` row points at the same `global_books` row — the
storage-savings invariant from ARCHITECTURE.md §4.

Phase 9 (below) puts the same pipeline behind a Celery task, but
`ingest.py` itself stays the synchronous source of truth — the task
calls it directly.

## Originals storage (Phase 31)

`worker/storage.py` persists every raw upload to the compose MinIO under
`originals/{book_id}/{sanitized-filename}` and `ingest.py` records that
key in `global_books.text_pointer` (plumbed since Phase 7, never filled
before this). New books upload *before* the `global_books` transaction
(a stored pointer never dangles); dup-hits backfill the existing row
only when its pointer is NULL — `UPDATE … WHERE text_pointer IS NULL`,
race-safe, never overwrites, never writes a second object. That backfill
is the **only recovery path** books ingested before Phase 31 will ever
get; do not weaken it.

**Client choice: minio-py (`minio>=7.2,<8`), not boto3.** Decided in
Phase 31: minio ships `py.typed` so pyright strict needs zero stub
packages or relaxation headers (boto3 is untyped and would force
`boto3-stubs` or another header); its footprint is ~5 small deps vs
botocore's ~80MB in a worker image Phase 16b fought to shrink; and it
speaks plain S3, so the future R2/B2 swap is endpoint + credentials in
`StorageSettings` (`SERMON_MINIO_*`, defaults match `infra/.env.example`
— host-side `localhost:9000`, never the compose-internal `minio:9000`).
Revisit only if a future phase genuinely needs AWS-ecosystem features
(transfer manager, presigned POST policies).

**Write-failure posture: fail the ingest loudly — both paths.** Storage
failures raise `OriginalsStorageError` and nothing catches it; the
Celery task fails and the operator sees it. Durability is the point of
the phase — log-and-continue would silently reintroduce the exact data
loss it exists to stop. On the new-book path the upload runs before
chunk/embed, so a failure aborts with nothing written anywhere. On the
dup-hit path the idempotent `user_library` upsert lands *first*, so a
loud backfill failure still leaves the user's library converged and the
NULL pointer retryable on the next dup-hit. Crash between upload and
Postgres commit leaves an orphan object — accepted, same posture as the
Milvus orphan-vector window above.

**Scope fence (B1): write-only.** NO read endpoint, NO presigned URLs,
zero new tenant read surface until the full-fidelity reader tier ships.
Anything that starts *reading* originals must re-run the tenant gates
(`/check-tenant-leak`, tenant-auditor, `make test-isolation`).

**Key hygiene.** The filename segment is client-supplied; it is
sanitized by `storage.sanitize_filename` — an exact mirror of
`api/uploads.py:_sanitize_filename` (mirrored, **not** imported:
worker/ must never import api/) plus a 255-char cap for object-key
safety. If one side's rules change, change the other in the same PR.
The bucket (`SERMON_MINIO_ORIGINALS_BUCKET`, default `sermon-originals`)
is created idempotently on first write.

## Celery (Phase 9)

`worker/celery_app.py` is the Celery app; `worker/tasks/ingest.py`
registers `tasks.ingest.ingest_book` — a thin adapter that unwraps
JSON-friendly arguments into `ingest()` from `worker/ingest.py`. Redis
is both broker (db 0) and result backend (db 1); connection settings
load from `SERMON_REDIS_*` in `infra/.env`.

```bash
make worker                                         # foreground prefork worker
make enqueue FILE=tests/samples/book.epub TENANT=tenant_a   # test producer
```

`TENANT` accepts a `users.user_id` UUID *or* a friendly label. Labels
are uuid5'd against a fixed namespace and the resulting user row is
upserted so the `user_library` FK resolves — local dev / verify only;
the Phase 10 API derives `user_id` from JWT, never from a payload.

**Reliability config** (in `celery_app.py`, with rationale in the
module docstring):

- `task_acks_late=True` + `task_reject_on_worker_lost=True` — a SIGKILL
  on the prefork child requeues the message; another worker (or the
  same parent's next child) picks it up. Verified by killing
  `ForkPoolWorker-1` mid-task; ForkPoolWorker-2 spawned and reprocessed
  the same task within ~10s.
- `broker_transport_options.visibility_timeout=300` — Redis broker
  redelivers an unacked message after 5 min if the *whole* worker
  process dies (so the parent can't reject). Default is 1h — too long
  for interactive verify.
- `worker_prefetch_multiplier=1` — ingest tasks run for minutes;
  prefetching would block reservations behind a single slow embed pass.
- `task_track_started=True` — the result backend reports `STARTED`
  (with worker PID + hostname) the moment a worker claims the task, so
  Phase 10's `GET /tasks/{id}` can distinguish "queued" from "running".

**Idempotency — the task-id claim (Phase 20).** The pipeline still
writes Milvus before the Postgres commit (Phase 12 made
`global_books` + `chunks` one transaction via
`ingest.py:_insert_book_with_chunks`, so Postgres lands both-or-neither),
and a crash after the Milvus flush and before that commit used to orphan
the attempt's vectors forever: the redelivered task found no committed
MinHash signature, missed the dedup gate, and re-ran under a fresh
`book_id`. Phase 20 closes that window for api-enqueued tasks with a
claim on the `upload_tasks` row (the row `POST /upload` commits before
`send_task`):

- The new-book path records its freshly minted `book_id` on the row
  (`ingest.py:_record_claim`) BEFORE the first non-transactional write
  (MinIO original, Milvus vectors). Keep that ordering — a claim written
  after the writes it covers is worthless.
- A redelivered task consults the claim first
  (`ingest.py:ingest_markdown`): committed `global_books` row → converge
  (upsert `user_library`, return duplicate); uncommitted → scrub the
  partial vectors (`_scrub_partial_vectors`) and re-run under the SAME
  `book_id`, which also overwrites the same
  `originals/{book_id}/{filename}` object key. **Invariant: a redelivered
  api-enqueued task converges to one consistent record — zero orphan
  vectors, zero duplicate vector sets, zero orphan originals objects.**
  The live-gated regression is
  `tests/test_ingest.py::test_task_claim_redelivery_converges`.
- Claim-less runs (manual CLI / `make enqueue` — no `upload_tasks` row)
  keep the legacy Phase 9 posture: `_record_claim` is a 0-row UPDATE and
  a mid-window crash orphans that attempt's vectors. Acceptable because
  those paths are operator-driven, not untrusted upload traffic.
- Residual (accepted): *concurrent* duplicate execution — the 300 s
  visibility timeout expiring under a still-RUNNING task redelivers it
  while the first attempt is alive; the claim is task-id-keyed, not
  leased, so attempts can interleave. Same exposure as before Phase 20.

## Dedup

`worker/dedup.py` is the MinHash LSH gate that sits between extract and
chunk in `ingest.py`. ARCHITECTURE.md §2 locks the algorithm: MinHash
LSH over lemmatized 5-shingles at Jaccard threshold 0.85, projected ~80%
storage savings at scale.

- `signature(markdown) -> MinHash` — tokenize, lowercase, WordNet
  lemmatize, emit a `MinHash(num_perm=128, seed=1)` over 5-shingles.
  Empty/short inputs return an empty MinHash (no candidates on lookup).
- `Dedup.find_duplicate(sig) -> uuid | None` — query the in-memory LSH.
  Stricter-than-construction thresholds post-filter by real Jaccard
  against `self._sigs`; looser thresholds raise.
- `Dedup.add(book_id, sig)` — update the in-memory LSH after a
  `global_books` insert.
- `dedup_index()` — `@lru_cache(maxsize=1)` process-singleton over
  `get_sync_session_factory()`.

**Persistence model.** The LSH is in memory; its inputs — one
`global_books.minhash_signature` row per book — are the persisted-in-
Postgres half. `Dedup._fetch_rows` rehydrates from those rows on first
call within a process; `Dedup._load_from(rows)` is the test seam that
takes an explicit row iterable so unit tests skip Postgres.

**NLTK WordNet.** Lemmatization needs the WordNet corpus (~40MB).
First call to `signature()` downloads it into `~/nltk_data/`. CI and
production workers should pre-warm by calling `signature("hello world "
"this is a test")` once at image-build time so request-path latency
doesn't carry the download. Tests that exercise `signature()` skip
cleanly when the corpus is absent.

## Database (db/)

Shared Postgres layer for `users`, `global_books`, `user_library`,
`highlights`, `collections`. Schema source of truth is
[`ARCHITECTURE.md` §4](../ARCHITECTURE.md#4-postgres-schema-sketch); this
package is the executable form. `api/` (Phase 10+) imports this module
directly — `worker.db` is the only cross-package import in the repo
(see the dep-direction rule in the root `CLAUDE.md`).

- `db/models.py` — SQLAlchemy 2.0 typed declarative models. UUID PKs are
  generated client-side (`default=uuid.uuid4`) to avoid a `pgcrypto`
  extension dependency. Timestamps are `DateTime(timezone=True)` with
  `server_default=func.now()` — naive datetimes silently miscompare,
  hence the `datetime.utcnow` ruff ban.
- `db/session.py` — async (`get_engine()` / `get_session_factory()`)
  and sync (`get_sync_engine()` / `get_sync_session_factory()`)
  singletons. FastAPI (Phase 10+) uses the async path; worker ingest
  (Phase 8) and Celery tasks (Phase 9) use the sync path — bridging via
  `asyncio.run` would leave loop-bound asyncpg connections stale across
  calls. Both engines share `DBSettings`; only the driver differs
  (`asyncpg` vs `psycopg`). Tests and Alembic build their own engines
  and should not touch the globals.
- `db/settings.py` — `DBSettings` (pydantic-settings, env prefix
  `SERMON_POSTGRES_`). The Make migrate targets source `../infra/.env`
  before invoking Python; a `KeyError` on a `SERMON_POSTGRES_*` var
  means the env was not sourced, not a missing default.

**Tenant invariant in the schema:** `user_library.book_id` is
`ON DELETE RESTRICT` (not CASCADE) so deleting a `global_books` row
cannot orphan another tenant's library entry — preserves the dedup
property under partial cleanup. `highlights` and `collections` cascade
on `user_id` delete (user owns those rows outright).

### Alembic

Config: `worker/alembic.ini` (run alembic from `worker/`). Scripts in
`db/alembic/`, versions in `db/alembic/versions/`. `env.py` overrides
`sqlalchemy.url` from `DBSettings` at runtime, so retargeting is
env-driven — do not write a connection URL into `alembic.ini`.

Initial migration (`0001_initial_schema.py`) is hand-written rather
than autogenerated so the repo is bootstrappable without a live
Postgres. Subsequent migrations should use `make migrate-new MSG=...`
and be reviewed against the `schema-reviewer` subagent (see below)
before merge.

### schema-reviewer subagent

`.claude/agents/schema-reviewer.md` is the Opus subagent that reviews
Alembic migrations for backward-compat and locking risk (NOT NULL adds,
`CREATE INDEX` without `CONCURRENTLY`, FK adds on populated tables,
enum/JSONB hazards, downgrade correctness, tenant-scoping coverage).

Invoke before merging any migration:

```
@schema-reviewer review the migrations under worker/db/alembic/versions/ on this branch
```

Findings halt the review — do not silently accept "ran fine locally"
as a pass on a hot-table migration.

## Tenant-isolation tooling

Two pieces ship in `.claude/` for ongoing audits as the codebase grows:

- **`.claude/agents/tenant-auditor.md`** — Opus subagent that reads
  diffs/files and verifies every Milvus search has `book_id IN (...)` in
  the filter, every `user_library` / `highlights` / `collections` query
  filters on `user_id`, and every `user_id` / `book_id` value is
  JWT-derived rather than client-supplied. Runs the Phase 3 isolation
  test as its final check.
- **`.claude/skills/check-tenant-leak/SKILL.md`** — model-disabled skill
  that runs the mechanical grep companion. Operator-invoked
  (`/check-tenant-leak`); both `CONTRIBUTING.md` and the PR template
  reference it.

Run both before merging anything that touches a Milvus or DB query, an
auth dependency, or the ingestion pipeline. They are checks, not
fixers — findings halt the audit; the fix is owner-decision territory.

## Retrieval (Phase 12)

`worker/retrieval.py` is the shared hybrid-retrieval kernel — dense
Milvus COSINE + sparse Postgres BM25, fused via Reciprocal Rank Fusion
(RRF, k=60). `api/search.py` wraps it for the HTTP layer; the worker
golden tests (`tests/test_retrieval_golden.py`) drive the same
primitives to gate retrieval regressions.

Public surface:

- `dense_search(client, query_vec, book_ids, limit)` — sync Milvus
  filter+search; emits `RetrievalHit`s with `dense_score` set.
- `bm25_search(session, query, book_ids, limit)` — **async** Postgres
  variant for the API.
- `bm25_search_sync(session, query, book_ids, limit)` — sync variant
  for worker tests + scripts.
- `rrf_fuse(dense, sparse, *, limit, k=60)` — merges two ranked lists
  by `(book_id, chunk_index)`, sums `1 / (k + rank)` per arm; returns
  top-K. Hit identity comes from the `metadata.chunk_index` that
  `ingest.py:_build_rows` writes into Milvus and the matching
  `chunks.chunk_index` column.
- `hybrid_search_sync(client, session, query, query_vec, book_ids, limit)`
  — convenience that runs both arms sequentially and fuses; used by
  the goldens. The async API parallelises the two arms via
  `asyncio.gather`.

**Tenant invariant:** both arms refuse empty `book_ids` (raise
`ValueError`) — the caller (`api/search.py`) short-circuits empty
libraries before hitting either index. ADR 0004 covers the BM25 backend
choice; ARCHITECTURE.md §3.5 is the schema source-of-truth.

### `chunks` table (Phase 12)

`worker/ingest.py` writes one `chunks` row per chunk alongside the
Milvus vector in the *same* Postgres transaction as `global_books`
(`_insert_book_with_chunks`). Pre-Phase-12 books need a one-time
backfill:

```bash
uv run python -m scripts.backfill_chunks          # all missing books
uv run python -m scripts.backfill_chunks --dry-run
uv run python -m scripts.backfill_chunks --book-id <uuid>
```

Idempotent via the `uq_chunks_book_chunk` unique constraint; safe to
re-run. The script reads from Milvus (`content_chunk` + `metadata`) so
it does not re-extract from disk.

### Maintenance scripts (Phase 21)

Two operator scripts share `backfill_chunks`' conventions (argparse,
`make_client()` + `get_sync_session_factory()`, per-book transactions,
Makefile targets sourcing `../infra/.env`) but invert its dry-run flag:
**they delete data, so the default is a dry-run and `--execute` applies**
(`make clean-parent-sections ARGS=--execute`, `make sweep-orphans
ARGS=--execute`).

- `scripts/clean_parent_sections.py` — strips HTML debris from stored
  `chunks.parent_section` and the matching Milvus `metadata.parent_section`
  by importing `chunking.clean_heading` (the capture-time sanitizer — see
  the Phase 21 invariant above). Milvus rows are rewritten via query →
  delete-by-id → reinsert because `library_vectors` has an `auto_id` PK
  and no partial JSON update; reinsert mints NEW vector ids, so nothing
  may assume Milvus id stability across a maintenance pass
  (`highlights.vector_id` is unwritten today).
- `scripts/sweep_orphans.py` — deletes Milvus vectors whose `book_id` has
  no `global_books` row, and `global_books` rows with zero `user_library`
  refs + zero chunks + zero vectors. Any tenant-reachable candidate aborts
  the whole run (exit 3); in-flight `upload_tasks` claims are skipped;
  Milvus exprs are built only from allowlist-validated book_ids.

Both are idempotent — a second run finds nothing to do.

## Milvus client init

`scripts/bootstrap_milvus.py:make_client` is the canonical pattern: read
`SERMON_MILVUS_HOST` and `SERMON_MILVUS_PORT` from env (defaults
`localhost:19530`) and construct `MilvusClient(uri=f"http://{host}:{port}")`.
Future ingest/search code should reuse it rather than re-deriving the URI.

The `library_vectors` schema lives in
[`ARCHITECTURE.md` §3](../ARCHITECTURE.md#3-milvus-schema--library_vectors);
`book_id` is the partition key — see
[§7.1](../ARCHITECTURE.md#71-dedup-vs-isolation-milvus-partition-key) for
the dedup-vs-isolation rationale (vectors shared globally per deduped book,
tenant scoping at the API via `book_id IN (<user's library>)`).

## Pyright LSP plugin (contributor tip)

Install once per machine so type errors surface inside Claude Code's loop
*in the same turn*, not just on the next `pyright` run:

```
/plugin install pyright-lsp@claude-plugins-official
/reload-plugins
```

The worker PostToolUse hook in `.claude/settings.json` still catches errors
without it — just on the next `Edit`/`Write` rather than while Claude is
reasoning.
