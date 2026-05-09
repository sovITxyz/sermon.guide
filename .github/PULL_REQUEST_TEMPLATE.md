<!-- Thanks for the PR. Fill in the sections below; delete what doesn't apply. -->

## What this changes

<!-- One or two sentences. Link to issue if any. -->

## Phase / area

<!-- Tick whichever apply. -->

- [ ] `infra/` — docker-compose / k8s
- [ ] `worker/` — ingestion pipeline (extract, dedup, chunk, embed)
- [ ] `api/` — FastAPI routes / auth / search
- [ ] `web/` — Next.js frontend
- [ ] `docs/` — ADRs, ARCHITECTURE, PHASES, READMEs
- [ ] tooling — `.claude/`, `.github/`, pre-commit, CI

## AI-collaboration checklist

<!--
This codebase is built primarily with AI assistants. The skills referenced
here are committed under .claude/skills/ and run as `/test-isolation`,
`/check-tenant-leak`, `/security-review` from any AI tool that supports
Claude Code skills.
-->

- [ ] Conventional commit messages, atomic per logical change
- [ ] `/test-isolation` run (search / auth / ingestion changes) — Phase 3+
- [ ] `/check-tenant-leak` run (DB / Milvus query changes) — Phase 6+
- [ ] `/security-review` run (any user-input handling) — built-in Claude skill
- [ ] Golden retrieval test added/updated (retrieval / ranking / chunking changes) — Phase 11+
- [ ] No variant-file litter (`_v2`, `_old`, `_fixed`, `_backup`, `_copy` suffixes) — pre-commit blocks these anyway
- [ ] `AGENTS.md` updated if conventions changed

## Test plan

<!-- How a reviewer can verify this works. Include commands. -->

```
# e.g. cd worker && uv run pytest
```

## Notes for reviewer

<!-- Anything subtle: tradeoffs taken, follow-ups deferred, ADR pending, etc. -->
