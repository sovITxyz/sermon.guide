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
| `ingest`            | `make ingest FILE=path/to/book.epub USER=u_alice BOOK=b_pilgrim` — single-book pipeline. `ARGS=--force` replaces existing vectors. |
| `migrate-up`        | `alembic upgrade head` against the docker-compose Postgres. Idempotent. |
| `migrate-down`      | `alembic downgrade -1` (one revision). `make migrate-down REV=base` wipes all the way. |
| `migrate-new`       | `make migrate-new MSG="describe change"` → `alembic revision --autogenerate`. Review the generated file before committing — autogenerate misses `server_default`, enum diffs, and may rename indexes cosmetically. |

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
1024-d model Phase 6 will use for the chunk embeddings written to Milvus.
Reusing one model keeps ingestion to a single ~1.3GB download. The first
`chunk()` call after a cold venv triggers that download via HuggingFace
Hub; subsequent calls hit the `HF_HOME` cache and the load is millisecond.
The end-to-end test gates on `~/.cache/huggingface/hub/models--BAAI--bge-large-en-v1.5/`
and skips when absent so CI doesn't block on a model fetch.

`Chunk` carries `(text, start_idx, end_idx, parent_section)`. `start_idx`
and `end_idx` are character offsets into the source markdown — they are
the citation anchor downstream. `parent_section` is the nearest ATX heading
above the chunk, best-effort; `None` for chunks before the first heading.

CLI (from `worker/`):

```bash
uv run python -m chunking path/to/book.md
```

The module is the single file `worker/chunking.py`; `python -m chunking`
runs it as `__main__` from the worker cwd (same pattern as `extractors`).

## Embedding

`worker/embedding.py` wraps `sentence-transformers` with the
**`BAAI/bge-large-en-v1.5`** model (locked in
[`ARCHITECTURE.md` §2](../ARCHITECTURE.md#2-locked-decisions)). `embed(texts)`
returns a `(N, 1024)` float32 array with each row L2-normalized — the
precondition for Milvus' `COSINE` metric to be inner-product in disguise
([§3](../ARCHITECTURE.md#3-milvus-schema--library_vectors)).

The model loads once per process via `@lru_cache`. First call after a cold
venv pulls ~1.3GB from HuggingFace Hub; subsequent calls hit `HF_HOME` and
load in a few seconds. Empty input returns a `(0, 1024)` array without
touching the model — CI's empty-input path never triggers a download.

Device is pinned to `"cpu"` for Phase 6; swap to `"cuda"` once a GPU
runtime exists. `torch` is sourced CPU-only in `pyproject.toml` for the
same reason — see the comment on the `torch` dep.

The Phase 5 semantic chunker (`chunking.py`) also loads BGE-Large for
boundary detection, via `llama-index-embeddings-huggingface`. The model
*file* is shared (one HF Hub cache entry); each loader keeps its own
in-memory copy. Consolidating to a single loader is a future micro-opt,
not Phase 6 scope.

**Offline mode.** Even with the cache warm, `sentence-transformers` makes
a HEAD request to HuggingFace Hub on load to check for PEFT adapter
files (BGE-Large has none) and to revalidate metadata. A DNS hiccup
during that call surfaces as `RuntimeError: Cannot send a request, as
the client has been closed.` mid-load — not the friendliest failure.

Set `HF_HUB_OFFLINE=1` (and `TRANSFORMERS_OFFLINE=1` for the inner
`transformers` calls) when you know the cache is warm and want a
deterministic, network-free load:

```bash
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
make ingest FILE=... USER=... BOOK=...
```

CI on GitHub Actions has full network access, so this is a local-dev /
flaky-Wi-Fi hint, not a default. Production Celery workers (Phase 9)
should set these in their pod env once the model is baked into the
image.

## Ingest

`worker/ingest.py` is the single-book ingest CLI for Phase 6. Pipeline:

```
detect → extract → chunk → embed → insert (partition: book_id)
```

```bash
uv run python -m ingest path/to/book.epub --user-id u_alice --book-id b_pilgrim
# or via make
make ingest FILE=path/to/book.epub USER=u_alice BOOK=b_pilgrim
```

`--user-id` is required so the calling contract is stable, but the
`user_library` row insertion is **deferred to Phase 8** (MinHash dedup —
inserts the row whether the book is new or a duplicate of an existing
`global_books`). The schema is in place as of Phase 7 (`db/models.py`).
`user_id` does NOT land in vector metadata — that would defeat the
dedup story per
[§7.1](../ARCHITECTURE.md#71-dedup-vs-isolation-milvus-partition-key);
vectors are shared globally per deduped book and tenant scoping happens
at the API at search time via `book_id IN (<user's library>)`.

Re-ingesting the same `book_id` is refused (`FileExistsError`) unless
`--force` is passed; with `--force`, existing rows for that `book_id`
are deleted before the new ones land. This is a stopgap until Phase 8's
MinHash LSH dedup makes "same book → same row set" a pipeline property.

No Celery yet (Phase 9) and no dedup yet (Phase 8) — single-process,
synchronous, one book per invocation.

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
- `db/session.py` — module-level `get_engine()` and
  `get_session_factory()` singletons over async `create_async_engine`.
  Tests and Alembic build their own engines and should not touch the
  globals.
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
