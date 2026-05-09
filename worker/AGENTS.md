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
