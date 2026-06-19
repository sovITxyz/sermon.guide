# Contributing to sermon.guide

Thanks for being here. This is an OSS project; contributions are welcome.

This codebase is built primarily with AI assistants (Claude Code, Cursor,
Aider, Codex, Copilot). All conventions live in `AGENTS.md` files so any
tool can pick them up. **If your AI keeps making the same mistake in this
repo, that's a docs bug — please file an issue against the relevant
`AGENTS.md`.** Don't fight your tools; fix the instructions.

## Setup

```bash
# Python: install uv (https://docs.astral.sh/uv/)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Node: install pnpm (https://pnpm.io/installation)
npm install -g pnpm

# Pre-commit hooks (gitleaks + variant-file guard)
pip install pre-commit
pre-commit install

# Bring up infra (Phase 1+)
make up
```

System packages: `pandoc` is required for EPUB extraction (`apt install pandoc`
on Debian/Ubuntu, `brew install pandoc` on macOS).

## Read these before opening a PR

- [AGENTS.md](./AGENTS.md) — repo-wide conventions
- [ARCHITECTURE.md](./ARCHITECTURE.md) — system design and locked decisions
- [docs/PHASES.md](./docs/PHASES.md) — phased build plan and what's currently in scope
- [docs/adr/](./docs/adr/) — historical decisions and their context
- The relevant `<package>/AGENTS.md` for the package you're touching

## Pre-PR checklist

Run these before pushing. The PR template will ask you to confirm them.

- [ ] **Conventional commits.** `feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`. One logical change per commit. No "WIP" or "fixes" commits — squash before opening the PR.
- [ ] **`/test-isolation`** — if you touched search, auth, or ingestion.
- [ ] **`/check-tenant-leak`** — if you touched any DB or Milvus query.
- [ ] **`/security-review`** — built-in Claude Code skill. Run for any change handling user input (uploads, search queries, API routes).
- [ ] **Golden retrieval test added** — if you changed retrieval, ranking, or chunking.
- [ ] **No variant-file litter.** Files named `*_v2.py`, `*_old.ts`, `*_fixed.tsx`, `*_backup.*`, `*_copy.*` are blocked by pre-commit. If you need to keep an old version around, branch.
- [ ] **`AGENTS.md` updated** — if you changed a convention, banned an API, added a tool, or learned something AI should know next time.
- [ ] **`docs/PHASES.md` row flipped** — if this PR closes a phase, tick the box and append completion date, branch, and deviations/follow-ups (see the header note in PHASES.md).

## Reporting bugs

File an issue using the bug template. For security issues, please email
security@sovit.xyz first; don't open a public issue until we've had a chance
to coordinate disclosure.

## Code of Conduct

This project adheres to the [Contributor Covenant 2.1](./CODE_OF_CONDUCT.md).
By participating, you agree to uphold it.

## License

By contributing, you agree that your contributions will be licensed under
the GNU Affero General Public License v3.0 (see [LICENSE](./LICENSE)).
