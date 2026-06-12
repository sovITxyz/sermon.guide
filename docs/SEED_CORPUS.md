# Seeding the production corpus

Operator runbook for going from **clean infra to a seeded public-domain
corpus**, reproducibly (Phase 23). What is allowed into the corpus and why
is [CORPUS_POLICY.md](./CORPUS_POLICY.md); this document is the how.

## TL;DR

```bash
# 0. live env (once) — see "Live-env recipe" before blindly running this
cp infra/.env.example infra/.env          # tracked defaults: PG 5432, Redis 6379, …
$EDITOR infra/.env                        # set DEEPINFRA_API_KEY=<operator key>

# 1. clean infra -> migrated, bootstrapped stack
make up                                   # compose: Postgres, Redis, Milvus(+etcd+MinIO)
make -C worker migrate-up
make -C worker bootstrap-milvus

# 2. download the manifest corpus (repo never holds the files)
#    -> see "Download the corpus"; files land in worker/tests/samples/

# 3. worker in one terminal, seeder in another
make -C worker worker                     # or --concurrency=1, see "Parallelism"
make -C worker seed-corpus ARGS=--dry-run # plan: 8 entries, all present?
make -C worker seed-corpus                # serial enqueue + wait, ~40 min/book

# 4. idempotency + integrity + tenant gates
make -C worker seed-corpus                # re-run: every book reports "duplicate"
make -C worker sweep-orphans              # dry-run auditor: zero candidates
make -C worker test-isolation             # 3/3 after the seed
# /check-tenant-leak                      # Claude Code skill, from the repo root
make -C worker test-live                  # keyed live gate: zero key/infra skips
```

Budget **~40 min/book on CPU** (sizes vary wildly — Calvin's *Institutes*
is an order of magnitude longer than *All of Grace*), so the serial
8-book seed is an afternoon. Remote-embedding spend is ~$0.006/book
(ADR 0006) — the whole corpus costs about a nickel.

## Prerequisites

- Host binaries: `pandoc`, `libmagic` (worker/AGENTS.md "System binaries").
- `cd worker && uv sync --all-extras --dev` done at least once.
- A DeepInfra API key (embeddings are remote since Phase 16b / ADR 0006).
- ~25 MB disk for the corpus files; the first ingest also downloads the
  ~40 MB NLTK WordNet corpus into `~/nltk_data/` (pre-warm cost, once).

## Live-env recipe

`infra/.env` (gitignored) is the single source of connection truth. Build
it from the tracked template plus exactly one secret:

```bash
cp infra/.env.example infra/.env
# then set DEEPINFRA_API_KEY=<your key> in infra/.env — the only required edit
```

**Port trap (Phase 21 finding iii):** the live compose Postgres listens on
**5432** (`SERMON_POSTGRES_PORT` in `infra/.env.example`) and Redis on
**6379**, but the *code defaults* are a dev box's old remaps —
`worker/db/settings.py` says 54322 and `worker/celery_app.py` says 63792.
Do **not** hardcode either: every make target in this runbook
(`migrate-up`, `worker`, `seed-corpus`, `sweep-orphans`, `test-live`)
sources `infra/.env` first, which is what makes the right ports win. If
you run any script bare (`uv run python -m scripts...`), source the env
yourself first — and never echo its values:

```bash
set -a; . infra/.env; set +a
```

A bare run without sourcing doesn't fail loudly — it points at dead ports
and turns into the exact silent-skip/connection-refused class Phase 23's
`test-live` target exists to kill.

## Download the corpus

The manifest (`worker/seeds/manifest.jsonl`) is the rights record: every
entry carries `source`, `source_id`, `source_url`, `license`, and the
expected `filename`. Files go into `worker/tests/samples/` — gitignored,
and deliberately the same directory the golden retrieval suite resolves
sample filenames from, so one download serves both the live seed and the
golden rows.

Explicit fetch lines (one-time downloads; be polite to the mirrors):

```bash
cd worker/tests/samples
curl -L -o confessions-augustine.epub                          'https://www.gutenberg.org/ebooks/3296.epub3.images'
curl -L -o institutes-of-the-christian-religion-calvin.epub    'https://www.ccel.org/ccel/s/calvin/institutes/cache/institutes.epub'
curl -L -o all-of-grace-spurgeon.epub                          'https://www.ccel.org/ccel/s/spurgeon/grace/cache/grace.epub'
curl -L -o sermons-on-several-occasions-wesley.epub            'https://www.ccel.org/ccel/s/wesley/sermons/cache/sermons.epub'
curl -L -o the-pilgrims-progress-bunyan.epub                   'https://www.gutenberg.org/ebooks/131.epub3.images'
curl -L -o the-imitation-of-christ-thomas-a-kempis.epub        'https://www.gutenberg.org/ebooks/1653.epub3.images'
curl -L -o the-practice-of-the-presence-of-god-brother-lawrence.epub 'https://www.gutenberg.org/ebooks/5657.epub3.images'
curl -L -o on-the-incarnation-athanasius.epub                  'https://www.ccel.org/ccel/s/athanasius/incarnation/cache/incarnation.epub'
```

Or generate the lines from the manifest (stays correct as entries are
added):

```bash
cd worker && uv run python - <<'EOF'
from scripts.seed_corpus import DEFAULT_MANIFEST, load_manifest
for b in load_manifest(DEFAULT_MANIFEST):
    print(f"curl -L -o {b.filename} '{b.download_url}'")
EOF
```

`make -C worker seed-corpus ARGS=--dry-run` confirms what is present
(missing files are skipped with their download URL — a partial seed is
fine and the next run picks up the stragglers).

## Run the seed

Terminal 1 — the worker (sources `infra/.env` itself):

```bash
make -C worker worker
```

Terminal 2 — the seeder:

```bash
make -C worker seed-corpus ARGS=--dry-run   # plan only: no DB/broker access
make -C worker seed-corpus                  # the real thing
```

The seeder pings the worker first (no worker → exit 2, nothing enqueued),
creates the `corpus-seed` owner row if missing, then **per book**: commits
a deterministic `upload_tasks` idempotency row, enqueues
`tasks.ingest.ingest_book` under a deterministic task id
(`uuid5(seed-namespace, sha256(file) + owner)`), and waits for the result
before enqueuing the next. Per-book output shows
`new book book_id=… (N vectors)` or
`duplicate — converged onto book_id=…`.

Exit codes: `0` all converged, `1` any ingest failed/timed out (or nothing
was enqueueable), `2` environment not wired.

### Ownership model

All seeded books are owned by the **`corpus-seed`** user
(`d296b559-28f8-54d6-9577-a5539913335c` — the deterministic identity
`make enqueue TENANT=corpus-seed` would also resolve). Seeding writes that
user's `user_library` rows and nothing else: no tenant's library changes.
The golden suite does not depend on the seeder's user at all — its fixture
re-ingests every referenced sample under its own deterministic golden
user, which on a seeded stack is a cheap dedup dup-hit that upserts the
golden user's own `user_library` row onto the shared `book_id`.

### Wall-clock and parallelism

Ingest is CPU-bound in semantic chunking (embeddings are remote): plan
**~40 min/book** average, hours for the largest entries. Estimate
`total ≈ 40 min × N books ÷ W workers` — but read this before raising W:

The broker redelivers any task unacked for >300 s
(`visibility_timeout`, celery_app.py), and a **free** worker slot will
happily start the redelivered copy of a **still-running** book. The Phase
20 claim converges *crashed* runs, not *concurrent* ones (task-id-keyed,
not leased — the documented residual), so an interleaved pair can double a
book's vectors. The safe configurations:

- **Default / recommended:** serial — `make -C worker seed-corpus`
  (`--max-in-flight 1`) with a single-slot worker
  (`uv run celery -A celery_app worker --loglevel=info --concurrency=1`
  from `worker/` with the env sourced; plain `make worker` defaults to
  one slot per CPU). With one slot, redelivered copies queue behind the
  running task and converge as dedup no-ops.
- **Parallel:** `ARGS='--max-in-flight N'` with worker `--concurrency=N`
  — every slot stays saturated mid-run, but the *tail* of the run (fewer
  books left than slots) reopens the free-slot window. If you parallelize,
  run the dedup-convergence checks below afterwards and treat any
  vector/chunk count mismatch as a re-seed signal (delete the affected
  book's vectors, re-run — the seeder converges).
- Never run two seeders at once, and never re-run the seeder while a
  previous run's tasks are still in flight.

## Re-run = no-op (idempotency)

Run the exact same command again:

```bash
make -C worker seed-corpus
```

Every book must report `duplicate — converged onto book_id=… (0 new
vectors)` — the Phase 8 MinHash gate short-circuits before chunk/embed and
the `user_library` upsert is `ON CONFLICT DO NOTHING`. Two deterministic
layers make re-runs safe even after a crash:

- **Content dedup** converges any re-run of *committed* books.
- **Task-id claim (Phase 20):** the seeder derives the Celery task id from
  `sha256(file)`, so a re-run after a mid-ingest crash re-presents the
  SAME task id, finds the `book_id` claim recorded on `upload_tasks`,
  scrubs the crashed attempt's partial vectors, and re-runs under the same
  `book_id` — no orphan vectors, no duplicate vector sets. (This is the
  claim path `make enqueue` does NOT have; the seeder exists so bulk
  ingest never runs claim-less.)

## Verify dedup convergence

Postgres counts (unchanged across re-runs):

```bash
docker compose -f infra/docker-compose.yml exec postgres \
  psql -U sermon -d sermon -c "
    SELECT (SELECT count(*) FROM global_books)  AS books,
           (SELECT count(*) FROM chunks)        AS chunks,
           (SELECT count(*) FROM user_library
             WHERE user_id = 'd296b559-28f8-54d6-9577-a5539913335c') AS seed_library;"
```

Per-book Milvus vector count == `chunks` row count (the invariant a
parallel-seed interleave would break):

```bash
cd worker && set -a && . ../infra/.env && set +a && uv run python - <<'EOF'
from scripts.bootstrap_milvus import COLLECTION_NAME, make_client
from scripts.sweep_orphans import _scan_milvus_counts
from sqlalchemy import text
from db import get_sync_session_factory

milvus = _scan_milvus_counts(make_client())
with get_sync_session_factory()() as s:
    pg = dict(s.execute(text("SELECT book_id::text, count(*) FROM chunks GROUP BY 1")).all())
bad = {b: (milvus.get(b, 0), pg.get(b, 0))
       for b in set(milvus) | set(pg) if milvus.get(b, 0) != pg.get(b, 0)}
print("MISMATCHES:", bad or "none — converged")
EOF
```

Canned auditor (zero candidates == zero orphan vectors / empty book rows):

```bash
make -C worker sweep-orphans      # dry-run by default
```

MinIO: exactly one `originals/<book_id>/<filename>` object per book
(console at `http://localhost:9001`, credentials from your `infra/.env`).

## Which test target gives which coverage

| target                          | env       | coverage                                                                                  |
| ------------------------------- | --------- | ----------------------------------------------------------------------------------------- |
| `make -C worker test`           | keyless   | fast unit suite; live-gated suites **skip silently BY DESIGN** — never read 100% green here as live coverage |
| `make -C worker test-live`      | keyed     | golden retrieval + ingest e2e (incl. kill-9 redelivery) + embedding weight-parity; **fails on any key/infra skip**; only the `corpus sample(s) missing` skip is tolerated |
| `make -C worker test-isolation` | infra     | Phase 3 tenant-isolation golden test (3/3) — required after any seed                       |
| `make -C worker test-retrieval-golden` | keyed | golden suite alone (subset of `test-live`)                                                |

After seeding, on the keyed dev box:

```bash
make -C worker test            # still passes keyless (unchanged posture)
make -C worker test-live       # zero key/infra skips; corpus rows run for real
```

## Post-seed tenant gates (non-negotiable)

Bulk ingest exercises dedup + library scoping, so after the seed:

```bash
make -C worker test-isolation        # 3/3
# then, from the repo root in Claude Code:
#   /check-tenant-leak
```

What they prove here: seeded books appear in **only** the `corpus-seed`
user's `user_library` (no tenant contamination), and dedup sharing at the
vector layer never cross-links libraries — a tenant search still filters
`book_id IN (<that user's library>)` and cannot surface seeded books it
doesn't own.

Quick manual contamination probe (expect `0` rows):

```bash
docker compose -f infra/docker-compose.yml exec postgres \
  psql -U sermon -d sermon -c "
    SELECT ul.user_id, count(*) FROM user_library ul
    JOIN global_books gb USING (book_id)
    WHERE ul.user_id <> 'd296b559-28f8-54d6-9577-a5539913335c'
      AND gb.created_at > now() - interval '1 day'
    GROUP BY 1;"
```

(Adjust the interval to bracket your seed run; golden-user rows appear
here only after you run the golden suite, which ingests under its own
user by design.)

## Adding a book to the corpus

1. Verify rights per [CORPUS_POLICY.md](./CORPUS_POLICY.md) (public domain
   only, `source_url` must demonstrate it).
2. Append a manifest line to `worker/seeds/manifest.jsonl` — the keyless
   unit tests audit it (`uv run pytest tests/test_seed_corpus.py`).
3. Download the file to `worker/tests/samples/` under the manifest's
   exact `filename`.
4. `make -C worker seed-corpus` — already-seeded books converge as
   duplicates; only the new book ingests.
5. If the book should be golden-gated, add a row to
   `worker/tests/golden/queries.jsonl` referencing the same filename.
