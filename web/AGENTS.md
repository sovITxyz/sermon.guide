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
  `tasks.ts`, `http.ts`, `summary.ts`, `reader.ts`, `reader-view.ts`,
  `library.ts` are pure (no `server-only`) so middleware and Vitest can
  import them.

## Editor — TipTap, code-split off non-editor routes (Phase 35; autosave Phase 36)

The sermon manuscript editor (`/sermons/[documentId]`) is a headless TipTap
contenteditable. Locked decisions:

- **MIT core ONLY — never a Pro extension (B2).** Deps:
  `@tiptap/react`, `@tiptap/pm`, `@tiptap/starter-kit`,
  `@tiptap/extension-placeholder` (all `^3`, MIT, React-19/Next-15 compatible —
  TipTap 3 is the current `latest`). `StarterKit` is `.configure({ link: false })`
  this phase: no link UI in the toolbar, and interactive links/citations arrive
  in Phase 37. Do **not** add a Pro/cloud extension or any paid `@tiptap-pro/*`.
- **Code-split.** `components/SermonEditor.tsx` is dynamic-imported via
  `next/dynamic` with `ssr: false` from the route's client shell
  (`app/sermons/[documentId]/SermonEditorShell.tsx`) — **the first and only
  `next/dynamic` use in web/.** This keeps the TipTap + ProseMirror bundle in its
  own chunk so it loads ONLY on the editor route, never on /library, /search,
  /read, or /upload. `ssr: false` is doubly required: TipTap's `useEditor` runs
  `immediatelyRender: false` per the App Router SSR rule, and ProseMirror touches
  the DOM. The server shell (`page.tsx`) fetches the full doc server-side
  (`lib/api-server.ts:getDocument`, bearer stays on the server) and passes it
  down, so the dynamic import defers the editor CODE, not the DATA.
- **ZERO `dangerouslySetInnerHTML`** (repo invariant). TipTap is headless
  contenteditable — it renders its own DOM from the ProseMirror document and the
  content round-trips as JSON, never as injected markup. List previews render the
  server-derived `content_text` (`preview` field) as PLAIN TEXT.
- **Autosave (Phase 36, B2 slice C — the editor stops losing work).** No Save
  button: the editor PATCHes `{title, content: editor.getJSON(),
  base_updated_at}` through the same-origin `/api/documents/[id]` proxy (which
  whitelists exactly those three fields — `lib/documents.ts`). The autosave
  pattern MIRRORS the Phase 33 reader position-persistence loop
  (`lib/reader-view.ts`: debounce + single-flight + pagehide keepalive +
  shouldPersist + adopt-server-value); the **pure, easy-to-get-wrong decisions
  live in `lib/sermon-autosave.ts`** (unit-tested in `test/sermon-autosave.test.ts`)
  and the **imperative loop (timers + fetch + 409 stop) stays in
  `components/SermonEditor.tsx`** (component-tested with fake timers in
  `test/components/SermonEditor.test.tsx`). Locked decisions:
  - **2 s debounce + 15 s max-interval** (`AUTOSAVE_DEBOUNCE_MS` /
    `AUTOSAVE_MAX_INTERVAL_MS`). Each edit resets the debounce; the first dirty
    edit since the last save arms the max-interval ceiling so a writer who never
    pauses still gets saved. Whichever fires first clears the other.
  - **ONE in-flight PATCH at a time.** Edits arriving during a flight are
    COALESCED into a single trailing save fired after it resolves — **never
    parallel PATCHes** (parallel writes race `base_updated_at` → spurious 409s).
    Encoded as the `FlightState` machine (`onSaveRequested`/`onFlightSettled`).
  - **Dirty check** (`isDirty`): an unchanged buffer (e.g. a selection-only
    `update`) never PATCHes. Compares title + a JSON serialization of content
    (TipTap returns a fresh object each `getJSON()`).
  - **Adopt the server value:** after every **200**, adopt the returned
    `updated_at` as the next `base_updated_at` (a `useRef`); reusing the stale
    load value manufactures self-conflicts.
  - **pagehide keepalive flush** via `fetch(..., {keepalive:true})`, only when
    dirty AND the serialized body is within the **~64 KB keepalive ceiling**
    (`KEEPALIVE_BODY_LIMIT`, `canKeepaliveFlush`). An oversize doc **SKIPS the
    flush silently** (status stays unsaved; the next open saves it) instead of
    throwing. Also flushed on unmount (SPA nav never fires pagehide).
  - **On 409: status=conflict, STOP the loop, show a banner** offering
    "Reload latest" — re-GET the doc, reset editor content + title +
    `base_updated_at` + dirty baseline, then resume. The user's buffer is KEPT
    until they choose; autosave is gated off (`conflicted` ref) so a stale tab
    never silently clobbers the other side. 413/404 surface a non-destructive
    error and autosave retries as the user keeps typing.
  - **SaveStatus indicator:** `saved` / `saving` / `unsaved` / `error` /
    `conflict` (an `aria-live="polite"` span with a `data-save-status` hook).
  - **No api change for the limiter.** PATCH `/documents` has no per-user
    rate-limit bucket (only signup/login per-IP and search-summary per-user are
    bucketed), so sustained ~1 PATCH/2s autosave is already unthrottled — see
    `api/AGENTS.md`. No bucket was added.

## Tests

Vitest runs **two projects in one `pnpm test`** (`vitest.workspace.ts`,
shared `resolve.alias` in `vitest.config.ts`):

- **`lib`** (node env, `test/**/*.test.ts`): pure-helper unit tests — cookie
  policy, validation, summary segmentation, task-status mapping. Relative
  imports, no DOM. Add one when you add a pure function.
- **`components`** (jsdom env, `test/components/**/*.test.tsx`): component tests
  via `@testing-library/react`. Setup in `test/components/setup.ts` (jest-dom
  matchers, RTL `cleanup`, a `next/link` → `<a>` stub). Uses
  `@vitejs/plugin-react` for the JSX transform.

**Phase 25 reversed the prior "pure helpers only / no jsdom" posture.** Rationale:
SearchPanel, the citation-chip renderer, and the upload flow had zero automated
coverage, so a regression in the chip href/label or the search submit shipped
silently. Component tests via `@testing-library/react` + the existing Vitest
runner reuse `pnpm test` with a minimal new dep surface (jsdom +
plugin-react + testing-library) — far less than Playwright component mode. The
two envs coexist via the workspace split, so pure-lib tests stay node-env and
green. (Browser E2E remains a separate concern — Playwright, Phase 25 E2E.)

**Adding a component test:** drop a `*.test.tsx` under `test/components/`.
It is auto-picked-up by the `components` project (jsdom + setup). Render with
RTL, stub `global.fetch` per-test (`vi.stubGlobal` / `installFetch` in
`test/components/helpers.ts`), assert real DOM and behavior (roles, attributes,
text) — not snapshots. For the 2 s upload poll / 1 s search ticker use
`vi.useFakeTimers()` + `await act(() => vi.advanceTimersByTimeAsync(ms))`; do
**not** mix `waitFor` with fake timers (it deadlocks — `waitFor` polls on a real
clock the fake timers freeze). Keep stubs typed (no `any`) so Biome's
`noExplicitAny` stays clean.

Dep versions (aligned to React 19.2 / Next 15.5 / Vitest 2.1 / Node 22):
`@testing-library/react@^16` (the React-19-compatible major — v15 and below
peer-depend on React 18), `@testing-library/jest-dom@^6`,
`@testing-library/user-event@^14`, `jsdom@^25`, `@vitejs/plugin-react@^4`.

## E2E (Playwright, Phase 25)

Browser regression tests for the hand-verified Phase 15/16 flows live in
`e2e/**/*.spec.ts` and run with `@playwright/test@^1.60`. They are SEPARATE
from `pnpm test` (Vitest): `pnpm test` = unit + component (jsdom);
`pnpm e2e` = Playwright. Vitest globs `test/**/*.test.{ts,tsx}` and Playwright
globs `e2e/**/*.spec.ts`, so the two never collect each other.

**Run it:**

- `pnpm e2e:install` — one-time `playwright install --with-deps chromium`.
- `pnpm e2e` — Playwright's `webServer` boots everything itself. No manual
  server juggling.

**Cold-start under CI.** The suite drives `next dev`, which compiles routes
on-demand on first hit. Under `CI=1` it runs `workers: 1` so each route
compiles once, sequentially — parallel workers would otherwise stack N
simultaneous cold compiles past the 30s default and flake the first cold run.
To absorb a single cold compile the config also raises `navigationTimeout`
(60s), `actionTimeout` (15s), the per-test `timeout` (90s), and the
`webServer.timeout` for `next dev`'s first boot (120s); `retries: 2` stays as a
backstop, not the primary fix. We deliberately keep the dev-server path (not
`next build`/`start`) — a genuinely-cold `rm -rf .next` first run is 4/4 green
with these limits. `reuseExistingServer` stays off in CI, on locally.

**Ports — never 3000.** The dev server binds **3100** (`E2E_WEB_PORT`, the
:3000 conflict on the dev box is real); the in-memory fake api binds **8081**
(`FAKE_API_PORT`). Both are env-overridable. `playwright.config.ts` points the
web server's server-only `API_BASE_URL` at the fake api so the same-origin
`/api/*` proxies reach it.

**Two backends, one config:**

- **Default / CI — fake api.** `e2e/support/fake-api.mjs` is an in-memory
  stand-in (no Python, no Postgres/Milvus/worker) that speaks the api's exact
  WIRE shapes: `/auth/{signup,login}`, a grounded `/search-summary` whose
  `[book:chunk]` markers exactly match the returned citations (so the Phase 24
  chip renderer resolves them), `/upload`, and `/tasks/{id}` with the Phase-20
  ownership-404 (own task → 200, another user's or unknown id → identical 404),
  and the Phase-35 `/documents` endpoints (POST create → 201 full doc, GET list →
  preview-only items with no `content` key, GET/`{id}` full, PATCH/`{id}` → 200 /
  **409 on a stale `base_updated_at`**, DELETE soft) — all bearer-scoped with the
  same uniform 404 for non-owned/unknown ids. A strictly-monotonic `updated_at`
  (a counter, not wall-clock) makes the optimistic-concurrency 409 deterministic.
  This is the documented CI boundary: the web CI job has no services, so the
  grounded-summary determinism the live path gets from the stub LLM is baked
  straight into the fake api. It exercises the real browser → same-origin proxy
  → HTTP contract end to end.
- **Live / nightly — real api + stub LLM.** Set `E2E_API_BASE_URL` to a booted
  real api and Playwright skips the fake api. Boot the api against a seeded
  corpus with the LLM round-trip short-circuited (no DeepInfra call, no ~134s
  wait) via the Phase-25 api knob `SERMON_API_LLM_BASE_URL`:

  ```sh
  # 1. deterministic OpenAI-compatible stub (echoes the prompt's markers back)
  node web/e2e/support/stub-llm.mjs                 # binds 127.0.0.1:8099
  # 2. real api pointed at the stub (a DUMMY key still satisfies the 503 guard;
  #    the stub ignores it). Source infra/.env first (never print it).
  set -a && . infra/.env && set +a
  cd api && PYTHONPATH=../worker:$PYTHONPATH \
    SERMON_API_LLM_PROVIDER=deepinfra DEEPINFRA_API_KEY=stub-key-ignored \
    SERMON_API_LLM_BASE_URL=http://127.0.0.1:8099/v1 \
    uv run uvicorn main:app --host 0.0.0.0 --port 8000 --no-proxy-headers
  # 3. point Playwright at it
  cd web && E2E_API_BASE_URL=http://127.0.0.1:8000 pnpm e2e
  ```

  `SERMON_API_LLM_BASE_URL` is a TEST-HARNESS-ONLY knob (api/settings.py docs
  the non-prod posture): it overrides the active provider row's hardcoded
  `base_url` so the summary LLM hits the local stub instead of the real
  provider. The live LLM path itself stays a manual/nightly concern (the spec).

**Test users** are throwaway (`e2e/support/users.ts`, random-UUID email +
password per run) — never commit real creds.

**Never assert on a bare `page.getByRole("alert")`.** This is an App Router app
and Next always renders an `<div role="alert" id="__next-route-announcer__">`,
so a bare `getByRole("alert")` matches TWO elements and trips Playwright's
strict-mode violation on every run. Scope alert assertions to the component's
error container — assert on the specific error copy
(`page.getByText("…exact message…")`) or filter the role by `hasText`/a
container locator. (Bit both `search.spec.ts` and `editor.spec.ts`.)

**Artifacts** (`test-results/`, `playwright-report/`, `blob-report/`,
`.playwright/`) are gitignored (`web/.gitignore`) and biome-ignored
(`biome.json`). CI uploads `playwright-report/` on failure.

## Tailwind / Biome

- Tailwind v3 utility classes inline; no CSS modules. Global directives live in
  `app/globals.css`.
- Biome is the formatter + linter (config `biome.json`): 2-space indent, double
  quotes, semicolons, 100-col. Run `pnpm format` before committing. Only add a
  `biome-ignore` for a rule that actually fires — an unused suppression is itself
  an error.

## Long-running proxies

`app/api/search-summary/route.ts` is the one proxy whose upstream call runs
minutes, not milliseconds (~134s warm E2E on the dev box: CPU rerank + LLM
round-trip). It carries an explicit `AbortSignal.timeout` (300s → 504) so a
wedged upstream can't hold the handler forever — copy that pattern for any
future long-running proxy. The UI side (`SearchPanel`) shows an elapsed-time
affordance instead of a bare spinner; a citation marker the model merges into
one bracket (`[A:70, A:51]`, Phase 14b finding) is **exploded into one chip per
resolvable member** by `lib/summary.ts:segmentSummary` (Phase 24 carries the
API's merged-member contract to the renderer). Each resolvable member becomes
its own linked chip; an invented member is dropped, and a bracket with no
resolvable member stays prose. (Pre-Phase-24 this section claimed merged
brackets "render as plain text" — that is no longer true.)

## Cross-package note

Phase 15 added `GET /library` to `api/` (the listing this frontend renders).
Phase 16 added `content` to `POST /search-summary` citations (the chunk
preview the citation cards render). Any new screen that needs backend data
needs a corresponding `api/` route — the frontend never reaches into
Postgres/Milvus, only HTTP.
