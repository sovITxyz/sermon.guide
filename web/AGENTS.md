# web/ — agent instructions

Per-package conventions for the Next.js 15 (App Router) frontend. See repo-root
[`AGENTS.md`](../AGENTS.md) and [`ARCHITECTURE.md`](../ARCHITECTURE.md). `web/`
is **fully independent** — it talks to `api/` over HTTP only and must NEVER
import Python packages (root `CLAUDE.md` dep-direction rule).

## Toolchain

- **pnpm, not npm.** `pnpm install`, `pnpm dev`, `pnpm test`. The lockfile is
  committed and CI runs `--frozen-lockfile`; regenerate with pnpm 9 (CI pins
  pnpm 9 via `pnpm/action-setup` — do **not** add a `packageManager` field to
  `package.json`, it conflicts with that pin).
- `pnpm typecheck` → `tsc --noEmit`. `pnpm lint` → `biome check`.
  `pnpm format` → `biome check --write` (formats + safe fixes + import sort).
- `tsconfig.json` is strict plus `noUncheckedIndexedAccess`,
  `noImplicitOverride`, `exactOptionalPropertyTypes`. An optional prop that may
  receive `undefined` must be typed `x?: T | undefined` (exactOptional).
- `next-env.d.ts` is **committed on purpose** (normally gitignored): CI runs
  `tsc --noEmit` without `next build`, so it needs Next's ambient types present.

## Auth flow — the JWT never reaches the browser

This is load-bearing; do not weaken it.

- Login proxies through `app/api/auth/login/route.ts`, which stores the API's
  JWT in an **HttpOnly, SameSite=Lax** cookie (`lib/session.ts`,
  `SESSION_COOKIE`). The token is **never** in the response body, never in
  `localStorage`, never readable by client JS.
- Every authenticated call to `api/` goes through a **route handler** under
  `app/api/**` (or a server component via `lib/api-server.ts`). The handler
  reads the cookie server-side and attaches `Authorization: Bearer …`. Client
  components only ever fetch same-origin `/api/*` — they never see the token or
  the API origin (`API_BASE_URL` is server-only, no `NEXT_PUBLIC_` prefix).
- `middleware.ts` is a **presence-only** gate (cookie exists → proceed). Real
  authorization is the API's JWT check; a server fetch that gets 401 throws
  `UnauthenticatedError` → the page redirects to `/login`.

## Server vs client components

- **Default to server components.** Data fetching happens server-side
  (`lib/api-server.ts:getLibrary` reads the cookie + calls `GET /library`).
- Add `"use client"` only for interactivity: forms (`AuthForm`), the uploader
  (`Uploader`), logout. Client code talks to `/api/*` route handlers, not to
  `api/` directly.
- `lib/` split: `config.ts` + `api-server.ts` carry `import "server-only"` (a
  build error if pulled into a client bundle). `session.ts`, `validation.ts`,
  `tasks.ts`, `http.ts` are pure (no `server-only`) so middleware and Vitest can
  import them.

## Tests

- Vitest, **pure helpers only** (`test/*.test.ts`, node environment, relative
  imports). Components/integration are covered by the live browser verify, not
  jsdom, to keep the dependency surface small. Add a unit test when you add a
  pure function (cookie policy, validation, status mapping).

## Tailwind / Biome

- Tailwind v3 utility classes inline; no CSS modules. Global directives live in
  `app/globals.css`.
- Biome is the formatter + linter (config `biome.json`): 2-space indent, double
  quotes, semicolons, 100-col. Run `pnpm format` before committing. Only add a
  `biome-ignore` for a rule that actually fires — an unused suppression is itself
  an error.

## Cross-package note

Phase 15 added `GET /library` to `api/` (the listing this frontend renders).
Any new screen that needs backend data needs a corresponding `api/` route — the
frontend never reaches into Postgres/Milvus, only HTTP.
