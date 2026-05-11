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
