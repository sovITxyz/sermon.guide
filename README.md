# sermon.guide

Multi-tenant ebook RAG platform for theological libraries and sermon
preparation. Upload your library; ask natural-language questions; get
1–2 paragraph grounded summaries with citations back to your own books.

> **Status:** v0 complete; v1/v2 work landed (through Phase 43 merged). See
> [docs/PHASES.md](./docs/PHASES.md) for the phased build plan and current
> progress.

## What it is

- 4,000-tenant scale design envelope, 10,000 books per tenant.
- Hybrid retrieval (dense BGE-Large + sparse BM25, RRF-fused, cross-encoder reranked, semantically pruned).
- Shared collection multi-tenancy with strict per-query isolation enforcement.
- AGPL-3.0 — see [LICENSE](./LICENSE).

For the full picture, read [ARCHITECTURE.md](./ARCHITECTURE.md).

## Quick start

### System dependencies

The worker shells out to two non-Python binaries that must be installed
separately. On Debian/Ubuntu:

```bash
sudo apt install pandoc libmagic1
```

On macOS: `brew install pandoc libmagic`. `pandoc` is used for EPUB →
Markdown conversion (and the Phase 43 .docx manuscript round-trip);
`libmagic` backs `python-magic` for MIME sniffing.

### Per-package commands

```bash
# bring infra up
make up

# bootstrap Milvus collection
cd worker && uv run python -m worker.scripts.bootstrap_milvus

# extract a book to Markdown
cd worker && uv run python -m extractors path/to/book.epub

# run the API
cd api && uv run uvicorn api.main:app --reload

# run the web frontend
cd web && pnpm dev
```

See [docs/PHASES.md](./docs/PHASES.md) for what's built and what's next.

## Repo map

| Path                  | What lives here                                            |
| --------------------- | ---------------------------------------------------------- |
| `infra/`              | docker-compose; future k8s manifests                       |
| `worker/`             | Python 3.12 ingestion pipeline (Celery + Redis)            |
| `api/`                | FastAPI backend; imports `worker.db`                       |
| `web/`                | Next.js 15 + Tailwind frontend                             |
| `docs/`               | ARCHITECTURE, PHASES, ADRs, reference PDFs                 |
| `.claude/`            | Claude Code skills, subagents, settings (committed)        |
| `.github/`            | PR template, issue templates, CI, CodeQL, Dependabot       |

## For contributors

- [AGENTS.md](./AGENTS.md) — repo-wide conventions for AI assistants and humans.
- [ARCHITECTURE.md](./ARCHITECTURE.md) — system design, locked decisions, open questions.
- [docs/PHASES.md](./docs/PHASES.md) — phased build plan and progress.
- [docs/adr/](./docs/adr/) — historical decisions in MADR format.
- [CONTRIBUTING.md](./CONTRIBUTING.md) — how to set up, test, and ship a PR.
- [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md) — Contributor Covenant 2.1.

## License

[AGPL-3.0](./LICENSE). If you run a modified version of sermon.guide as a
network service, you must publish your changes.
