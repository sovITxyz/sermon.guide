# sermon.guide — agent instructions

Cross-tool instructions for Claude Code, Cursor, Aider, Codex, Copilot, etc.
Per-package conventions live in `<package>/AGENTS.md` and are added as each
phase lands.

## What this is

Multi-tenant ebook RAG platform — theological libraries + sermon prep. See
[ARCHITECTURE.md](./ARCHITECTURE.md) for the system design and locked
decisions; see [docs/PHASES.md](./docs/PHASES.md) for the phased build plan
and progress.

## Stack

- **worker/** — Python 3.12, uv, ruff (E,F,W,I,B,BLE,TRY,ASYNC,S,ARG,ERA,UP,TID), pyright strict. Ingestion pipeline + Celery workers.
- **api/** — Python 3.12, FastAPI. Imports `worker.db` (shared models). Same lint/type stack as worker.
- **web/** — Next.js 15 (app router), TypeScript strict (`noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`), Tailwind, Biome, pnpm.
- **infra/** — docker-compose for v0 (Milvus + etcd + MinIO + Redis + Postgres). K8s/KEDA later.

## Repo layout & dependency direction

```
api/    → imports worker.db (shared schema/session)
worker/ → no upward deps
web/    → fully independent; talks to api/ over HTTP only
infra/  → no code; compose files, env templates, future k8s manifests
```

`web/` must NEVER import Python packages. `worker/` must NEVER import `api/`.
The only cross-package import is `api/` → `worker.db`.

## Branches and commits

- **Branch naming:** `phase-N/short-slug` (e.g. `phase-3/tenant-isolation-test`). One branch per phase.
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/), atomic. `feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`. One logical change per commit — multiple commits per branch are fine, monolithic "phase N done" commits are not.
- **Don't `git push --force` to `main`.** Don't bypass hooks (`--no-verify`). Both are blocked in `.claude/settings.json` for Claude Code; the rule applies to humans too.

## Tenant isolation is not negotiable

This is a multi-tenant system. Every Milvus search MUST include
`tenant_id == "<jwt_user_id>"` in `expr`. Every SQLAlchemy query against
`user_library`, `highlights`, or `collections` MUST filter by `user_id`
derived from the JWT, never from the request body or query params.

Before merging anything that touches a Milvus or DB query, run:

- `/check-tenant-leak` — grep-based audit (Phase 6).
- `tenant-auditor` subagent — semantic audit (Phase 6).
- `make test-isolation` in `worker/` — golden isolation test (Phase 3).

If the changes are non-trivial, run all three.

## Pre-PR checklist

See `CONTRIBUTING.md` for the full version. Short form:

- [ ] Conventional commit messages
- [ ] `/test-isolation` if you touched search, auth, or ingestion
- [ ] `/check-tenant-leak` if you touched DB or Milvus queries
- [ ] `/security-review` (built-in Claude skill) if you touched user-input handling
- [ ] No variant-file litter (`_v2`, `_old`, `_fixed`, `_backup`, `_copy` suffixes are blocked by pre-commit)
- [ ] `AGENTS.md` updated if conventions changed

## When in doubt

- Architecture questions → [ARCHITECTURE.md](./ARCHITECTURE.md).
- "Is this decision settled?" → [ARCHITECTURE.md §7 Open Questions](./ARCHITECTURE.md#7-open-questions).
- "Why this DB / model / approach?" → [docs/adr/](./docs/adr/).
- Phase scope or what's next → [docs/PHASES.md](./docs/PHASES.md).
- Repeating the same mistake here? That's a docs bug. File an issue against this file.
