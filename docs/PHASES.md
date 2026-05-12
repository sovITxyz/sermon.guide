# sermon.guide — Phased Build Plan

## Progress

After each phase commit, tick the box and append: completion date, branch name, deviations/follow-ups.

- [x] Phase 0 — Repo skeleton + OSS scaffolding + ARCHITECTURE.md (completed 2026-05-09, branch `phase-0/repo-skeleton`. LICENSE: AGPL-3.0 selected.)
- [x] Phase 1 — Infrastructure (docker-compose) + infra/AGENTS.md (completed 2026-05-09, branch `phase-1/infra-compose`. Modern Compose schema, no top-level `version:`. `make up` brings up Postgres 16, Redis 7, Milvus standalone v2.6.15 + etcd v3.5.25 + MinIO RELEASE.2024-05-28; `--wait` blocks until all healthcheck-gated. Re-up after `make down` measured at ~22s.)
- [x] Phase 2 — Milvus collection bootstrap + Python tooling (completed 2026-05-09, branch `phase-2/milvus-bootstrap`. §7.1 resolved Option B — `book_id` partition; vectors shared globally per book; tenant scoping enforced at API via `book_id IN (user_library)` filter; `tenant_id` field dropped from schema. Worker PostToolUse hook live; Pyright LSP plugin recommended for in-turn type-error feedback.)
- [x] Phase 3 — Tenant isolation smoke test (HARD GATE) + /test-isolation skill (completed 2026-05-11, branch `phase-3/tenant-isolation-test`. Reconciled to §7.1 partition-on-`book_id`: two simulated tenants are two disjoint `book_id` sets, filter is `book_id in [...]` not `tenant_id`. Local gate via `cd worker && make test-isolation`; worker CI skips cleanly when Milvus unreachable (autouse fixture socket-probes port). Mutation test verified — dropping `filter=` produces 2 loud failures with the failure-mode docstring. CI-blocking variant deferred to Phase 11 when `retrieval-golden` job also needs live Milvus.)
- [x] Phase 4 — Format detection + extraction (completed 2026-05-11, branch `phase-4/format-extraction`, PR #9 rebase-merged. EbookLib → pandoc for EPUB, pymupdf4llm for PDF; MIME-sniffed via libmagic, never file extension. Sample-gated end-to-end tests skip cleanly without local copyrighted EPUB/PDF fixtures. System deps `pandoc` + `libmagic1` documented in README and `worker/AGENTS.md`. Upload-side hardening (size limits, libmagic content-vs-claim mismatch) deferred until the ingestion pipeline grows a network edge.)
- [x] Phase 5 — Semantic chunking (completed 2026-05-12, branch `phase-5/semantic-chunking`. No deviations; end-to-end verified locally with the BGE-Large cache prewarmed.)
- [ ] Phase 6 — Embedding + Milvus insert + tenant-auditor subagent
- [ ] Phase 7 — Postgres schema + Alembic migrations + schema-reviewer subagent
- [ ] Phase 8 — MinHash LSH dedup
- [ ] Phase 9 — Celery worker
- [ ] Phase 10 — FastAPI skeleton + JWT auth + upload + api/AGENTS.md
- [ ] Phase 11 — Vector search endpoint + golden-test infrastructure
- [ ] Phase 12 — Hybrid search (BM25 + RRF)
- [ ] Phase 13 — Cross-encoder rerank + semantic highlighting
- [ ] Phase 14 — Gemini 1.5 Flash summary agent
- [ ] Phase 15 — Next.js: auth + library + web/AGENTS.md
- [ ] Phase 16 — Next.js: search + summary UI

---

Each phase is **one new Claude Code session**. Phases are intentionally small to keep context tight and reduce drift / errors. Run sequentially; verify each deliverable before moving on.

## Reference materials
PDFs live in `~/Downloads/`:
- `Ebook Search and Library System Architecture.pdf` — research paper
- `Future Platform_ Ebook RAG Architecture V1.pdf` — blueprint
- `Next Steps_ Platform Implementation Roadmap V1.pdf` — roadmap

Phase 0 distills these into a committed `ARCHITECTURE.md` inside the repo. Subsequent phases read that file, not the PDFs.

## Workflow per phase
1. Open new Claude Code session.
2. Copy the prompt for the current phase from `docs/PHASES.md` (or from this file pre-Phase-0).
3. Verify the deliverable.
4. Tick the phase's checkbox in `docs/PHASES.md` — completion date, branch name, any deviations or follow-up notes.
5. Commit.
6. Move to the next phase.

Global `CLAUDE.md` is loaded automatically (git identity, branch hygiene, /effort MAX). After Phase 0, a root `AGENTS.md` (with repo-level `CLAUDE.md` symlinked to it — Linux Foundation cross-tool standard so Cursor/Codex/Aider/Gemini/Copilot share the same instructions) plus per-package `AGENTS.md` files load contextually as Claude works in each directory. Each phase's branch name is pre-suggested so all sessions follow the same convention.

**Source of truth.** Once Phase 0 lands, `docs/PHASES.md` (committed in the repo) becomes canonical for both the plan and progress state — visible to contributors on clone and to any AI session. The personal `~/sermon-guide-phases.md` you bootstrapped from can be archived or deleted at that point.

---

## Phase 0 — Repo skeleton + OSS scaffolding + ARCHITECTURE.md

```
# Bootstrap sermon.guide

Set up a fresh OSS repo for sermon.guide — a multi-tenant ebook RAG platform (4,000 tenants × 10,000 ebooks each, theological library + sermon prep use case). Solo dev now; future contributors will use mixed AI tools (Claude Code, Cursor, Aider, Codex, Copilot) so all conventions go in cross-tool AGENTS.md files.

## Reference PDFs (read all three first)
- ~/Downloads/Ebook Search and Library System Architecture.pdf
- ~/Downloads/Future Platform_ Ebook RAG Architecture V1.pdf
- ~/Downloads/Next Steps_ Platform Implementation Roadmap V1.pdf

## Steps

1. Confirm the repo path with me. Default suggestion: ~/projects/sermon.guide.

2. mkdir parent if needed, git init inside, empty initial commit on main, then create branch `phase-0/repo-skeleton`.

3. Directory layout:
   - infra/      — docker-compose, k8s later
   - worker/     — Python ingestion pipeline
   - api/        — FastAPI backend
   - web/        — Next.js frontend
   - docs/       — PDFs copied here for offline reference
   - docs/adr/   — Markdown Any Decision Records (MADR format)
   - .claude/    — settings, skills, agents (committed to repo)
   - .github/    — PR template, issue templates, workflows

4. Write `ARCHITECTURE.md` at repo root:
   - Goal + scale targets.
   - Locked decisions: tenancy = shared collection w/ metadata filtering; vector DB = Milvus flat index; embeddings = BGE-Large 1024d; ingestion = Celery+Redis on K8s w/ KEDA; format = EbookLib+pandoc / pymupdf4llm; dedup = MinHash LSH; chunking = LlamaIndex SemanticSplitter; search = hybrid BGE+BM25 with RRF + cross-encoder rerank; pruning = BGE-M3 semantic highlighting; LLM = Gemini 1.5 Flash; frontend = Next.js+Tailwind; raw storage = R2/B2.
   - Milvus collection schema for `library_vectors` (id PK INT64, vector FloatVector 1024, tenant_id VarChar partition key, book_id VarChar, content_chunk Text, metadata JSON).
   - Postgres schema sketch (Users, GlobalBooks, UserLibrary, Highlights, Collections per the research PDF).
   - Out of scope for v0 — multi-region, mobile, Graph RAG, semantic caching, highlight import.
   - **## Open Questions** section listing decisions to make:
     - Dedup vs isolation: partition on tenant_id (vectors duplicated per user) OR partition on book_id (vectors shared, query filters book_id IN userlibrary). Decide before Phase 2.
     - Highlights: separate Milvus collection or same collection with content_type field? Decide before Phase 11.
     - LICENSE: Apache-2.0 (permissive + patent grant) OR AGPL-3.0 (network-copyleft, prevents proprietary SaaS forks). Decide before publishing publicly. MIT not recommended for SaaS-shaped projects.

5. Write `AGENTS.md` at repo root. Hard cap 300 lines, aim for ~60. Include only what AI cannot derive from code:
   - One-line project description and stack pointer.
   - Monorepo layout + dep direction (api imports worker.db; web is fully independent).
   - Conventional commits (atomic, one logical change per commit). Branch naming: `phase-N/short-slug`.
   - Per-package conventions live in `<package>/AGENTS.md` (added in later phases).
   - Pointers to `ARCHITECTURE.md` and `docs/adr/`.
   - For each line ask: would removing this cause AI to make mistakes? If no, cut.

6. Symlink so Claude Code finds the same content natively: `ln -s AGENTS.md CLAUDE.md` (run from repo root).

7. Write `.claude/settings.json` (committed to repo):
   - `permissions.allow`: `Bash(docker compose *)`, `Bash(make *)`, `Bash(uv run *)`, `Bash(pytest *)`, `Bash(pnpm *)`, `Bash(npm run *)`, `Bash(gh pr *)`, `Bash(gh api *)`, `Bash(git status)`, `Bash(git diff*)`, `Bash(git log*)`, `Bash(git show*)`.
   - `permissions.deny`: `Read(.env)`, `Read(.env.*)`, `Read(~/.ssh/**)`, `Read(~/.aws/**)`, `Read(~/.gnupg/**)`, `Bash(git push origin main*)`, `Bash(git push --force*)`, `Bash(rm -rf*)`.
   - `enableAllProjectMcpServers: false` — critical for OSS, blocks malicious MCPs from cloned dependency repos.
   - `PostToolUse` hooks (stub now, real commands wire in once tooling exists):
     - matcher `Edit|Write`, filePattern `worker/**/*.py|api/**/*.py` → `cd <pkg> && uv run ruff check --fix "$file" && uv run pyright "$file"` (Phase 2 enables for worker, Phase 10 for api).
     - matcher `Edit|Write`, filePattern `web/**/*.{ts,tsx}` → `cd web && pnpm tsc --noEmit && pnpm biome check "$file"` (Phase 15 enables).
   - `PreToolUse` hook on `Bash`: grep guard that exits 1 on `rm -rf` or `git push.*--force`.
   - Add `.claude/settings.local.json` to `.gitignore` for per-machine overrides.

8. Write `LICENSE`. **Stop and ask me which license** (Open Question above). Do not pick for me.

9. Write `CONTRIBUTING.md`:
   - Setup: install `uv` for Python, `pnpm` for Node, `pre-commit install` for hooks, `make up` for infra.
   - Pre-PR checklist: `/test-isolation` (search/auth/ingestion changes), `/check-tenant-leak` (DB/Milvus query changes), `/security-review` (built-in Claude skill — for any user-input handling), conventional commits, no variant-file litter.
   - Note: this codebase is built primarily with AI assistants. If AI keeps making the same mistake in your work, that's a docs bug — file an issue against `AGENTS.md`.

10. Write `CODE_OF_CONDUCT.md` — Contributor Covenant 2.1 verbatim.

11. Write `.github/` files:
    - `PULL_REQUEST_TEMPLATE.md`: "What this changes" / phase area checkboxes / AI-collaboration checklist (`/test-isolation`, `/check-tenant-leak`, `/security-review`, golden test added if retrieval changed, no `_v2`/`_old`/`_fixed` filenames, AGENTS.md updated if conventions changed) / test plan.
    - `ISSUE_TEMPLATE/bug.md` and `ISSUE_TEMPLATE/feature.md`.
    - `workflows/ci.yml` — lint + typecheck + tests. Gate per-package jobs (worker/api/web/retrieval-golden) with a single `filter` job that probes the filesystem for each entry point (`worker/pyproject.toml`, `api/pyproject.toml`, `web/package.json`, `worker/tests/golden/queries.jsonl`) and emits a boolean output per package; downstream jobs use `needs: filter` + `if: needs.filter.outputs.<pkg> == 'true'` so they SKIP cleanly until each phase wires its package in. **Never put `hashFiles()` in a job-level `if:`** — it is only valid in step-level `if:`; at job level it causes GitHub Actions to reject the entire workflow at load time (workflow fails in 0s on every push, CI signals silently lost). Mirror the filter-job pattern in `workflows/codeql.yml`.
    - `workflows/codeql.yml` — default GitHub-managed Python + JS config.
    - `dependabot.yml` — weekly Python (uv) + npm updates, grouped.

12. Write `.gitleaks.toml` (default ruleset) and `.pre-commit-config.yaml` with:
    - gitleaks hook
    - variant-file regex hook blocking commits matching `_(old|new|v2|backup|fixed|copy)\.(py|ts|tsx)$`

13. Write the first three ADRs in `docs/adr/` using MADR (https://adr.github.io/madr/):
    - `0001-vector-db-choice.md` — Milvus vs Weaviate/Qdrant/pgvector.
    - `0002-tenancy-model.md` — record both partition options + state the open question.
    - `0003-embedding-model-choice.md` — BGE-Large 1024d.

14. Write `docs/PHASES.md` — the in-repo source of truth for plan + progress. Copy the contents of `~/sermon-guide-phases.md` (the file the user bootstrapped from) verbatim, then prepend a `## Progress` section with all 17 phases as unchecked checkboxes:
    ```
    - [ ] Phase 0 — Repo skeleton + OSS scaffolding + ARCHITECTURE.md
    - [ ] Phase 1 — Infrastructure (docker-compose) + infra/AGENTS.md
    - [ ] Phase 2 — Milvus collection bootstrap + Python tooling
    ... (through Phase 16)
    ```
    Add a one-line note at the top of the Progress section: "After each phase commit, tick the box and append: completion date, branch name, deviations/follow-ups." This document is now canonical; future sessions read `docs/PHASES.md`, not the personal copy.

15. Write `README.md` (brief: what this is, current phase pulled from `docs/PHASES.md`, how to run, links to AGENTS.md, ARCHITECTURE.md, CONTRIBUTING.md, PHASES.md).

16. Write `.gitignore` (Python, Node, .env, model caches, Milvus volumes, .DS_Store, `.claude/settings.local.json`).

17. Copy the three PDFs into `docs/`.

18. **Tick Phase 0's checkbox in `docs/PHASES.md`** — append completion date and `phase-0/repo-skeleton` as the branch. Note "LICENSE deferred pending Open Question answer" if the user hasn't picked yet.

## Verify
- `actionlint .github/workflows/*.yml` reports clean. (Install via `go install github.com/rhysd/actionlint/cmd/actionlint@latest` or the release binary at https://github.com/rhysd/actionlint/releases.)
- Push the branch and confirm CI actually executes: `gh run list --branch phase-0/repo-skeleton --limit 5` shows the `CI` and `CodeQL` workflows completing cleanly (success, or clean skips for jobs whose package files don't exist yet) — NOT failing in 0s. **Local YAML parsing (`python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`) is necessary but NOT sufficient** — it accepts syntax GitHub Actions rejects at workflow-load time (e.g. `hashFiles()` or `matrix` in job-level `if:`), which is exactly how a parse-time bug can ship without the validator noticing. Only an observed workflow run proves CI is alive.

19. Commit. Stop.

## Stop criteria
Repo exists. ARCHITECTURE.md, AGENTS.md (+ CLAUDE.md symlink), .claude/settings.json, LICENSE, CONTRIBUTING.md, CODE_OF_CONDUCT.md, .github/ templates and workflows, .gitleaks.toml, .pre-commit-config.yaml, three ADRs, **docs/PHASES.md with Phase 0 ticked**, all committed. Open Questions captured. Don't proceed until I've answered the LICENSE question.
```

---

## Phase 1 — Infrastructure (docker-compose) + infra/AGENTS.md

```
cd to the sermon.guide repo. Read ARCHITECTURE.md and root AGENTS.md before doing anything.

Goal: `docker compose up -d` boots all infra deps.

## Build
- Branch: phase-1/infra-compose off main.
- infra/docker-compose.yml with:
  - Milvus standalone (latest stable) + etcd + MinIO (deps)
  - Redis 7
  - Postgres 16
- Healthchecks on every service.
- Named volumes for persistence; .env for ports/passwords (commit .env.example, gitignore .env).
- Makefile at repo root: up, down, logs, ps, nuke (down -v).
- Write `infra/AGENTS.md` (~30 lines): which services live here, healthcheck conventions, env-var naming (`SERMON_*` prefix), how to add a service, why we're not using docker-compose v1 syntax.

## Verify
- `make up` → all services healthy within 60s.
- `make down && make up` → idempotent.
- Reachability: curl Milvus health, redis-cli ping, psql connect.

Commit. Stop. No Python yet.
```

---

## Phase 2 — Milvus collection bootstrap + Python tooling

```
cd to sermon.guide. Read ARCHITECTURE.md.

**Pre-flight:** confirm the dedup-vs-isolation Open Question in ARCHITECTURE.md is resolved. If not, stop and ask which approach to take — it changes the schema partition key.

Goal: Python script creating the `library_vectors` Milvus collection per spec, plus the worker package's lint/typecheck stack so AI assistants get fast feedback.

## Build
- Branch: phase-2/milvus-bootstrap off main.
- worker/pyproject.toml (uv, Python 3.12). Deps: pymilvus, ruff, pyright.
- Ruff config: line-length=100, target-version=py312, select = E, F, W, I, B, BLE, TRY, ASYNC, S, ARG, ERA, UP, TID. flake8-tidy-imports.banned-api: `datetime.datetime.utcnow` → "use datetime.now(tz=UTC)"; `pickle` → "use json or msgpack — pickle is unsafe across versions".
- Pyright config: typeCheckingMode = "strict", pythonVersion = "3.12".
- worker/scripts/bootstrap_milvus.py:
  - Connect to Milvus (host/port from env).
  - Create `library_vectors` per ARCHITECTURE.md.
  - Partition key per the resolved Open Question.
  - Flat index on vector field (per locked decision).
  - Idempotent: --force drops+recreates; default skips if exists.
- worker/Makefile: bootstrap-milvus, lint (ruff check), format (ruff format), typecheck (pyright), test.
- Activate the worker PostToolUse hook in `.claude/settings.json` so ruff + pyright actually run on edits inside `worker/`.
- Install Pyright LSP plugin so type errors land in Claude's loop in-turn rather than next run: `/plugin install pyright-lsp@claude-plugins-official`.
- Wire CI: `.github/workflows/ci.yml` worker job runs `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`, `uv run pytest`.
- Write `worker/AGENTS.md` (~40 lines): uv usage, ruff/pyright commands, banned-api list and why, async-Milvus client gotchas, where pymilvus client is initialized, link to ARCHITECTURE.md for schema, mention LSP plugin so contributors install it.

## Verify
- `uv run ruff check` clean. `uv run pyright` clean.
- Run bootstrap script. List collections + describe schema. All fields and partition key correct.
- Run again without --force → skip cleanly. Run with --force → recreate cleanly.
- Edit a worker file with `datetime.datetime.utcnow()` → PostToolUse hook fires and surfaces the banned-api error in the same turn.

Commit. Stop.
```

---

## Phase 3 — Tenant isolation smoke test (HARD GATE) + /test-isolation skill

```
cd to sermon.guide. Read ARCHITECTURE.md.

This is a hard gate. Multi-tenant data leakage is the #1 architectural risk. Don't move past this until isolation is provably solid.

## Build
- Branch: phase-3/tenant-isolation-test off main.
- Add deps: pytest, numpy.
- worker/tests/test_tenant_isolation.py:
  - Setup: insert 100 random vectors as tenant_a, 100 as tenant_b, distinguishable book_ids.
  - Test 1: filtered query as tenant_a → zero tenant_b ids in results.
  - Test 2: same in reverse.
  - Test 3: query without filter returns mixed (sanity).
  - Behavior-named class `TestTenantIsolation` with failure-mode docstring on each test.
  - Teardown: drop test data only, not the collection.
- worker/Makefile: test-isolation.
- Ship `.claude/skills/test-isolation/SKILL.md` so any contributor (Claude, Codex, Cursor, Gemini) can `/test-isolation`. Frontmatter: `name: test-isolation`, `description: Run multi-tenant isolation tests after schema or query changes`. Body: invoke the make target; halt and report on failure; never paper over — tenant isolation failures are critical bugs.

## Verify
- `make test-isolation` passes.
- Deliberately break the filter (remove the expr arg). Test must fail loudly with the docstring's failure-mode message visible. Restore.
- In a fresh Claude Code session: `/test-isolation` invokes correctly and reports.

Commit. Stop. Don't move on if anything looks off.
```

---

## Phase 4 — Format detection + extraction

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: extract clean Markdown from EPUB and PDF inputs. Text only, no embedding.

## Build
- Branch: phase-4/format-extraction off main.
- Deps: python-magic, EbookLib, pypandoc, pymupdf4llm.
- Pandoc binary required (apt install pandoc). Document in README and worker/AGENTS.md.
- worker/extractors/__init__.py — detect(path) -> "epub"|"pdf".
- worker/extractors/epub.py — EbookLib → (X)HTML → pandoc → markdown.
- worker/extractors/pdf.py — pymupdf4llm.to_markdown.
- worker/extractors/extract.py — dispatcher: extract(path) -> str.
- CLI: `python -m worker.extractors.extract <path>` prints markdown to stdout.

## Test data
- worker/tests/samples/ — public-domain EPUB (Project Gutenberg) and a PDF. If missing, ask me to drop one in.

## Verify
- EPUB sample → readable markdown, no alt-text pollution / metadata leakage.
- PDF sample → readable markdown, line-wrapping reasonable.
- Smoke test in worker/tests/test_extractors.py (assert non-empty + sane char distribution).

Commit. Stop. No chunking, no embedding.
```

---

## Phase 5 — Semantic chunking

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: split extracted markdown into semantic chunks for embedding.

## Build
- Branch: phase-5/semantic-chunking off main.
- Deps: llama-index, llama-index-embeddings-huggingface (SemanticSplitter needs an embedder for boundary detection).
- worker/chunking.py:
  - chunk(markdown: str) -> list[Chunk] using LlamaIndex SemanticSplitterNodeParser.
  - Chunk dataclass: text, start_idx, end_idx, parent_section (best-effort from markdown headers).
- CLI: `python -m worker.chunking <md_file>` prints chunk count + previews.

## Verify
- Run on Phase 4's EPUB output → 50–500 chunks for a typical novel; boundaries on sentence ends.
- worker/tests/test_chunking.py smoke test.

Commit. Stop.
```

---

## Phase 6 — Embedding + Milvus insert + tenant-auditor subagent

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: end-to-end ingest CLI: file → chunks → BGE embeddings → Milvus rows partitioned by book_id (per ARCHITECTURE.md §3 + §7.1 resolution). Plus ship the `tenant-auditor` subagent and paired `/check-tenant-leak` skill so future sessions can audit tenant scoping on demand.

## Build
- Branch: phase-6/embedding-insert off main.
- Dep: sentence-transformers (or FlagEmbedding).
- worker/embedding.py:
  - Load BAAI/bge-large-en-v1.5 once at module init. CPU fine for now; document GPU swap.
  - embed(texts: list[str]) -> np.ndarray (N, 1024).
- worker/ingest.py:
  - CLI: `python -m worker.ingest <file> --user-id <id> --book-id <id>` (user_id used for the user_library row that records ownership; book_id is the vector partition).
  - Pipeline: detect → extract → chunk → embed → insert with metadata JSON (filename, chunk index).
- No dedup yet, no Celery. Single-process.
- Ship `.claude/agents/tenant-auditor.md`:
  - Frontmatter: `name: tenant-auditor`, `description: Audit code for tenant-scoping and isolation bugs`, `tools: Read, Grep, Bash(uv run pytest worker/tests/test_tenant_isolation.py *)`, `model: opus`.
  - Body: every Milvus search has `book_id IN (<set>)` in expr where the set is sourced from Postgres user_library for a JWT-authenticated user (never the request body); every SQLAlchemy query filters by user_id derived from JWT, never the request; API routes derive user_id from JWT only; highlights queries are double-scoped (user_id AND book_id). Run isolation test as final check.
- Ship `.claude/skills/check-tenant-leak/SKILL.md` (the grep-based check that CONTRIBUTING.md and the PR template reference). Frontmatter: `name: check-tenant-leak`, `description: Audit codebase for unscoped DB or vector queries`, `disable-model-invocation: true`. Body: grep `collection.search(`, `session.query(`, `.execute(`; verify each Milvus search has a `book_id IN` expression and each SQLAlchemy query has a `user_id` filter; flag any `user_id` or `book_id` set sourced from request body rather than JWT-derived (and the user_library lookup driven by it).

## Verify
- Ingest a real book under tenant_a. Row count = chunk count.
- Re-run Phase 3 isolation test with this real data present — must still pass.
- Invoke the `tenant-auditor` subagent against current code — should pass clean.

Commit. Stop.
```

---

## Phase 7 — Postgres schema + Alembic migrations + schema-reviewer subagent

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: relational schema for users, books, libraries, highlights — per the research PDF. Plus ship the `schema-reviewer` subagent so future migrations get reviewed for backward compat and locking risk.

## Build
- Branch: phase-7/postgres-schema off main.
- Deps: sqlalchemy, alembic, asyncpg, pydantic-settings.
- worker/db/ (shared layer; api/ will import this):
  - models.py: Users, GlobalBooks (with minhash_signature blob + text_pointer), UserLibrary, Highlights, Collections.
  - session.py: async engine + session factory.
- worker/db/alembic/ — config + initial migration creating all tables.
- worker/Makefile: migrate-up, migrate-down, migrate-new MSG=...
- Ship `.claude/agents/schema-reviewer.md`:
  - Frontmatter: `name: schema-reviewer`, `description: Review Alembic migrations for backward compat and locking risk`, `tools: Read, Grep, Bash(uv run alembic *)`, `model: opus`.
  - Body checklist: NOT NULL adds without default on existing tables; index creation on hot tables (require CREATE INDEX CONCURRENTLY); enum changes (PG enums are awful); foreign-key cascades; data backfills inside DDL transactions; downgrade path correctness; tenant scoping preserved on every new table.

## Verify
- migrate-up from clean DB → all tables.
- migrate-down → all gone.
- migrate-up again → idempotent.
- Run `schema-reviewer` against initial migration — expect "first migration, no compat concerns".

Commit. Stop. No business logic in models — schema only.
```

---

## Phase 8 — MinHash LSH dedup

```
cd to sermon.guide. Read ARCHITECTURE.md and the dedup section of the research PDF.

Goal: skip embedding work for books we've already seen.

## Build
- Branch: phase-8/minhash-dedup off main.
- Dep: datasketch.
- worker/dedup.py:
  - signature(markdown: str) -> MinHash (5-shingles, lemmatized).
  - find_duplicate(sig, threshold=0.85) -> book_id | None — query GlobalBooks signatures via LSH.
  - LSH index persisted in Postgres; lazy-load and rebuild from DB on worker start.
- Update worker/ingest.py:
  - After extract, before chunking: compute signature, check dedup.
  - If duplicate: insert UserLibrary row pointing at existing GlobalBooks.book_id. Skip chunking + embedding.
  - If new: chunk + embed, then insert GlobalBooks row with signature.

## Verify
- Ingest book X under tenant_a → vectors created.
- Ingest same book under tenant_b → no new vectors, just UserLibrary pointer.
- Confirm tenant_b can search the shared content per the dedup-vs-isolation decision from Phase 0. If isolation is broken, stop and revisit the Open Question.
- Run `tenant-auditor` subagent — must still pass.

Commit. Stop.
```

---

## Phase 9 — Celery worker

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: turn the ingest CLI into a Celery task fed by Redis.

## Build
- Branch: phase-9/celery-worker off main.
- Deps: celery, redis.
- worker/celery_app.py — Celery instance, broker = Redis, backend = Redis.
- worker/tasks/ingest.py — @app.task wrapping the Phase 6/8 pipeline.
- worker/Makefile: worker (runs celery worker), enqueue FILE=... TENANT=... (test enqueue).

## Verify
- Start worker with `make worker`.
- In another shell: `make enqueue FILE=... TENANT=tenant_a` → task picked up, completes, vectors land.
- Kill worker mid-task → restart picks it up cleanly OR marks failed (whichever the design specifies).

Commit. Stop. KEDA/k8s autoscaling is later.
```

---

## Phase 10 — FastAPI skeleton + JWT auth + upload + api/AGENTS.md

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: HTTP layer. Sign up, log in, upload files (which queue Celery tasks).

## Build
- Branch: phase-10/fastapi-auth-upload off main.
- api/pyproject.toml — fastapi, uvicorn, python-jose[cryptography], passlib[bcrypt], python-multipart, sqlalchemy, asyncpg, celery, redis. Import worker.db models (shared package).
- Copy the same Ruff + Pyright strict config from worker/pyproject.toml. Wire api into the PostToolUse hook (`.claude/settings.json` already targets `api/**/*.py`).
- api/main.py — FastAPI app, CORS, healthz.
- api/auth.py:
  - POST /auth/signup — create User, hashed password.
  - POST /auth/login — return JWT.
  - get_current_user dependency that decodes JWT.
- api/uploads.py:
  - POST /upload — multipart file, save to local storage (R2/B2 later), enqueue Celery ingest with user_id from JWT, return task_id.
  - GET /tasks/{task_id} — Celery task status.
- api/Makefile: dev, test, lint, typecheck.
- Wire CI: add api lint/typecheck/test job alongside worker.
- Write `api/AGENTS.md` (~40 lines): `user_id` is always derived from JWT, never from request body or query params; the user's `book_id` set for any vector search is resolved server-side from `user_library` per request, never accepted from the client; auth dependency injection pattern; how to add a new route; common 401/403 mistakes; reference `tenant-auditor` subagent before merging any new query; reference `schema-reviewer` for any DB query changes.
- Reinforce `/security-review` (built-in Claude Code skill) usage in `CONTRIBUTING.md` and `.github/PULL_REQUEST_TEMPLATE.md` for any PR touching api/ or web/ that handles user input. (PR template already includes the checkbox from Phase 0; this phase is when contributors actually start running it.)

## Verify
- Sign up two users.
- Log in as user_a, upload book → task_id.
- Poll /tasks/{id} → succeeds.
- Reuse user_a's JWT after logout-style scenarios → 401 where expected.
- Run `/security-review` against the new code — fix any reported issues before commit.
- Run `tenant-auditor` against api/ — clean.

Commit. Stop. No search yet.
```

---

## Phase 11 — Vector search endpoint + golden-test infrastructure

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: authenticated semantic search over the user's own ingested books. Plus stand up the golden-test infrastructure so retrieval regressions get caught in CI when models, chunking, or ranking change.

## Build
- Branch: phase-11/vector-search off main.
- api/search.py:
  - POST /search → {query: str, limit: int = 10}.
  - Embed query with BGE-Large.
  - Milvus filtered search per the resolved tenancy model.
  - Return list of {content_chunk, book_id, metadata, score}.
- Shared embedding loader (don't duplicate model init across processes).
- Golden-test infrastructure:
  - `worker/tests/golden/queries.jsonl` — JSONL rows: `{"query": "...", "expected_book_ids": [...], "min_score": 0.7}`. Seed with 5–10 hand-curated entries against the public-domain test corpus (Augustine, Bunyan, Bonhoeffer if available — pick books a sermon-prep user would actually search).
  - `worker/tests/test_retrieval_golden.py` — load JSONL, run search per row as a fixed test tenant, assert at least one expected book_id in top-K with score ≥ min_score. Hit/miss binary, no fuzzy partial credit. Behavior-named class `TestRetrievalAccuracy`.
  - Wire into CI as a separate job `retrieval-golden` so a regression is visible distinct from unit tests.

## Verify
- As user_a: search a phrase from user_a's book → matching chunks.
- As user_b: same phrase → nothing (or only user_b matches).
- Phase 3 isolation test still passes.
- Golden tests pass on current corpus. Deliberately break ranking (e.g., disable BGE, return random vectors) → goldens fail loudly. Restore.

Commit. Stop. No reranking, no BM25.
```

---

## Phase 12 — Hybrid search (BM25 + RRF)

```
cd to sermon.guide. Read ARCHITECTURE.md and hybrid-search section of research PDF.

Goal: combine dense + sparse retrieval via Reciprocal Rank Fusion.

## Build
- Branch: phase-12/hybrid-search off main.
- BM25: simplest path is Postgres tsvector on a chunks table (ingest writes here too — backfill if needed). Document the choice in ARCHITECTURE.md and add an ADR `0004-bm25-backend.md`.
- api/search.py:
  - Dense + sparse run in parallel.
  - RRF fusion: score = Σ 1/(k + rank_i), k=60.
  - Return fused top-K.

## Verify
- Specific name query (e.g., "Theodore Roosevelt") that vector misses → BM25 catches it.
- Thematic query → still works.
- Golden tests still pass; add 1–2 entries that specifically exercise BM25 strengths.

Commit. Stop.
```

---

## Phase 13 — Cross-encoder rerank + semantic highlighting

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: tighten relevance and prune context before LLM call.

## Build
- Branch: phase-13/rerank-highlight off main.
- Dep: sentence-transformers cross-encoder.
- api/rerank.py — top-30 from hybrid → cross-encoder/ms-marco-MiniLM-L-6-v2 → top-10.
- api/highlight.py — sentence-level scoring with BGE-M3 against query, prune below 0.5 threshold.

## Verify
- Manual eyeballing: pruned chunk content is more on-topic.
- Token count post-prune drops 70–80% (architecture target).
- Golden tests still pass; reranking should improve hit rate, not degrade it.

Commit. Stop.
```

---

## Phase 14 — Gemini 1.5 Flash summary agent

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: 1–2 paragraph thematic summary endpoint.

## Build
- Branch: phase-14/summary-agent off main.
- Dep: google-genai.
- api/summary.py:
  - POST /search-summary → {query, limit_chunks: int = 20}.
  - Run Phases 12–13 retrieval pipeline.
  - Prompt: query + pruned chunks with citation markers (book title + page/chunk index).
  - Gemini 1.5 Flash with grounding instructions: "1–2 paragraphs, cite [book:chunk] inline, only use provided context."
  - Return {summary, citations: [...]}.
- GOOGLE_API_KEY in env. Update .env.example. Add to `.claude/settings.json` deny list as `Read(.env*)` already covers it.

## Verify
- Real query like "what does this say about faith" → coherent grounded output with citations that map to retrieved chunks.
- Hallucination check: query nothing-in-corpus → response says so, doesn't confabulate.

Commit. Stop.
```

---

## Phase 15 — Next.js: auth + library + web/AGENTS.md

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: minimal frontend for the auth/upload/library flow. Search UI is Phase 16.

## Build
- Branch: phase-15/web-auth-library off main.
- web/ — Next.js 15 app router, TypeScript, Tailwind, Biome.
- tsconfig.json: `strict`, `noUncheckedIndexedAccess`, `noImplicitOverride`, `exactOptionalPropertyTypes`.
- Pages: /signup, /login, /library, /upload.
- JWT in HttpOnly cookie via Next route handlers (don't expose to client JS).
- /library: fetch UserLibrary entries + ingestion task status, render table.
- /upload: drag-and-drop, POST /upload, poll /tasks/{id}, optimistic update on library.
- Activate the web PostToolUse hook in `.claude/settings.json` so `pnpm tsc --noEmit && pnpm biome check $file` runs on edits inside web/.
- Wire CI: add web job running `pnpm tsc --noEmit`, `pnpm biome check`, `pnpm vitest run`.
- Write `web/AGENTS.md` (~40 lines): server vs client component split rules; HttpOnly cookie auth flow; never store JWT in localStorage; data-fetching patterns; tailwind conventions; `pnpm` (not npm) for deps; biome run command; route handler pattern for proxying api/ calls so JWT cookie never reaches the browser JS.

## Verify
- Browser: sign up, log in, upload book, watch status flip to done, see book in library.
- `pnpm tsc --noEmit && pnpm biome check` clean.
- Run `/security-review` — fix any reported issues.

Commit. Stop.
```

---

## Phase 16 — Next.js: search + summary UI

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: the actual sermon-prep workflow.

## Build
- Branch: phase-16/web-search off main.
- /search page:
  - Query input, submit.
  - Calls /search-summary.
  - Renders 1–2 para summary at top, citation cards below (book title, chunk preview).
  - Loading / empty / error states.
- Add to nav from /library.

## Verify
- Full browser flow: log in, upload theology book, wait for ingest, search "what does this say about grace" → grounded summary with working citations.
- Run `/security-review` — fix any reported issues.

Commit. Stop. v0 done.
```

---

## Beyond Phase 16

Future work, not in v0:
- R2/B2 raw file storage (replace local).
- KEDA + k8s manifests (replace docker-compose).
- Highlight/note import (Kindle, Logos exports).
- Hierarchical / parent-document retrieval.
- Semantic query caching.
- Per-tenant rate limits.
- Graph RAG.
- Postmortem dir (`agent_docs/postmortems/`) — populate as AI mistakes surface in real work.
- Additional MCP servers (GitHub MCP, Context7) — opt in deliberately, keep `enableAllProjectMcpServers: false`.

Plan these phases when v0 is in your hands and you know what's actually missing.
