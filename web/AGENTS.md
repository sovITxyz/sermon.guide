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
- **Dev-server port — :3000 is occupied on the dev box.** `pnpm dev` defaults
  to :3000, which conflicts with another long-running service there; run
  `pnpm dev --port 3001` instead. (E2E uses its own :3100 — see the E2E
  section below.) **Never `pkill -f 'next dev'` unqualified** — it can kill
  unrelated dev servers; stop the dev server by its specific PID/port.
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

## Citations — the in-editor citation node (Phase 37, B2 slice D)

The signature B2 integration: a cited library passage from search becomes a
first-class manuscript block that deep-links into the reader. The custom TipTap
node lives in `components/editor/`. Locked decisions:

- **Block-level atom TipTap node `citation`** (`components/editor/CitationNode.tsx`,
  `Node.create`). Schema: `group: "block"`, `atom: true` (no editable children —
  the card is placed, not typed into), `selectable: true`, `draggable: false`.
  Registered in `SermonEditor.buildExtensions()` so a stored doc containing a
  citation parses on load. `Node`, `mergeAttributes`, `ReactNodeViewRenderer`,
  `NodeViewWrapper`, and the `NodeViewProps`/`ReactNodeViewProps` types come from
  **`@tiptap/react` (v3 re-exports)** — **do NOT add `@tiptap/core`** as a direct
  dep, and never a Pro extension (the MIT-only rule still holds).
- **Attrs (`addAttributes`):** `bookId` (string), `chunkIndex` (number),
  `bookTitle` (string), `snippet` (string), `parentSection` (string \| null,
  nullable). Each has `parseHTML`/`renderHTML` mapping to a `data-*` attribute
  (`data-book-id`, `data-chunk-index`, `data-book-title`, `data-snippet`,
  `data-parent-section`) on a `div[data-type="citation"]` wrapper. **The
  load-bearing contract is the JSON round-trip:** `editor.getJSON()` -> persist
  to `documents.content` -> `setContent()` -> re-render must preserve every attr.
  TipTap rebuilds attrs from JSON via `addAttributes` automatically; the `data-*`
  mapping additionally survives an HTML clipboard round-trip and lets an existing
  doc parse on load. Tested with a **real headless `Editor`** (no @tiptap mock)
  in `test/components/CitationNode.test.tsx`.
- **`bookTitle` + `snippet` are CACHED at insert** from the search hit. The node
  view (`CitationView`, a `ReactNodeViewRenderer`) renders PURELY from
  `node.attrs` and **NEVER fetches on render** — so the doc stays self-contained
  even if the book leaves the library or its text changes. A raw `/search` hit
  has **no title** (see `lib/types.ts:SearchHit`); the drawer must source
  `bookTitle` from the one-shot `/library` set (next builder's job). The drawer
  inserts via `editor.chain().insertContent({ type: "citation", attrs })`, which
  fires the existing autosave `update` (no autosave change needed).
- **Card styling mirrors the /search citation card** (`SearchPanel.tsx` Sources
  `<li>`): a bordered card with title, a `section · chunk N` meta line (section
  via `lib/summary.ts:displaySection`, which drops `<`-bearing EPUB tag soup),
  and the **snippet as PLAIN TEXT** (`line-clamp-4 whitespace-pre-wrap`) — **ZERO
  `dangerouslySetInnerHTML`** (repo invariant). When owned, the card carries a
  `Read in context` link to `readHref(bookId, chunkIndex)` =
  `/read/{bookId}?chunk={chunkIndex}` (`rel="noopener"`).
- **Degraded badge via ONE shared library lookup — NOT per-citation fetches.**
  `app/sermons/[documentId]/page.tsx` fetches the user's library ONCE on doc open
  (`getLibrary()`), passes the owned-`book_id` **string[]** to the shell (a `Set`
  can't cross the RSC boundary), which rebuilds the `Set` and hands it to the
  editor as `ownedBookIds`. `SermonEditor` wraps `<EditorContent>` in
  `LibraryMembershipProvider` (`components/editor/library-membership.tsx`); every
  `CitationView` reads the set with `useLibraryMembership()` to decide
  owned-vs-degraded — **ZERO per-citation network calls** (the verify's hard
  requirement). Context reaches the node view because `ReactNodeViewRenderer`
  portals render inside the `<EditorContent>` React subtree. If `bookId` is not
  in the set (or no provider — the default is an empty set, so everything
  degrades safely), the card drops the link and shows a `No longer in your
  library` badge; the cached snippet still renders (degraded is additive UI,
  never content-hiding). A failed `/library` fetch on the page is non-fatal — it
  degrades every card rather than blocking the editor.
- **`/api/search` proxy** (`app/api/search/route.ts`, `lib/search.ts`
  `whitelistSearch`): the drawer's same-origin entry. Whitelists `{query}` ONLY
  (drops `limit`/`rerank`/smuggled `user_id`/`book_ids`), cookie -> bearer
  server-side, returns RAW `/search` hits (no LLM). Tenant scoping is the API's
  (`/search` resolves `book_id`s from the JWT user's library); the proxy adds NO
  unscoped query. Mirrors `search-summary/route.ts` but with a 60s timeout
  (`/search` is fast/LLM-free, not the 300s summary path).
- **In-editor LibraryDrawer** (`components/editor/LibraryDrawer.tsx`): the UI that
  turns a library search into an inserted citation. **Opened from a toolbar
  affordance** — a `+ Citation` button in `SermonEditor` (`aria-label="Cite from
  your library"`, `aria-expanded`) that toggles the drawer; it is **closed by
  default** so the editor opens uncluttered, and the drawer has its own `Close`.
  It reuses the SearchPanel plumbing (same-origin POST, `searchQueryProblem`
  client validation, the `mounted` guard) but hits `/api/search` — RAW hits, NO
  LLM — so it is FAST: a plain `Searching your library…` label, **no** minutes-long
  elapsed ticker (that is the `/search-summary` path only). Hits render as
  **selectable rows** (`data-testid="library-drawer-hit"`) showing the book title
  + a `section · chunk N` meta line + a 2-line snippet preview, all PLAIN TEXT.
  - **Hit -> citation attrs mapping (the design gap).** A raw `/search` hit
    (`lib/types.ts:SearchHit`) has NO title and no field named `snippet`, so the
    drawer maps: `bookId <- book_id`, `chunkIndex <- metadata.chunk_index`,
    `parentSection <- metadata.parent_section`, `snippet <- content_chunk`
    (**cached at insert** — the doc stays self-contained), and `bookTitle <- the
    one-shot {book_id -> title} map`. A hit whose book is not in the map falls
    back to `Untitled book` so the card still renders.
  - **`bookTitle` source — the same one-shot `/library` fetch.**
    `page.tsx` projects the library to `{book_id, title}[]` (`LibraryBookRef`,
    plain JSON crosses the RSC boundary) and passes it to the shell, which derives
    BOTH the owned-`book_id` `Set` (degraded badge) AND a `book_id -> title` `Map`
    (drawer titles) and hands them to `SermonEditor` as `ownedBookIds` +
    `bookTitles`. **No extra fetch** — one `/library` call backs both.
  - **Insert** = `editor.chain().focus().insertContent({ type: "citation", attrs
    }).run()` on a row click. This fires the editor `update` event the **Phase 36
    autosave already handles** (debounce + single-flight) — **no autosave change**,
    and the node is in `editor.getJSON()` so it survives save -> reload. The
    drawer stays open after an insert (cite several passages in one search).
  - Tested in `test/components/LibraryDrawer.test.tsx` (search -> renders raw
    hits; row click -> `insertContent` with the mapped attrs incl. the
    untitled-fallback + null `parent_section`; plain-text snippet; proxy `{error}`
    surfaced; empty-query no-fetch) and end-to-end in `e2e/editor.spec.ts` (open
    drawer -> search -> insert -> card shows title + snippet + Read-in-context
    link -> autosave -> reload persists). The fake api gained `/search` (raw hits)
    and `/library` (owned set + titles), bearer-scoped, with `book_id`s that match
    so an inserted citation resolves as OWNED.

## Sermons list — delete + restore (Phase 36, B2 slice C)

`components/SermonList.tsx` is a **client island** (it was a pure server
component through Phase 35) — server components cannot mutate, so the row
actions live in the island and call `router.refresh()` after each mutation to
re-run the `/sermons` server component against the new state. `app/sermons/page.tsx`
stays a server component that fetches the list (`getDocuments`) and passes it
down. Locked decisions:

- **Soft delete is confirm-gated.** A manuscript is irreplaceable, so the row's
  Delete button fires `window.confirm` first; a dismissed confirm fires **no**
  request. On accept it `DELETE`s the same-origin `/api/documents/[id]` proxy.
  A **204** (success) and the uniform **404** (already gone) are both treated as
  "no longer listed" → refresh; any other status surfaces a non-destructive
  inline error and does **not** refresh.
- **Restore reachability = an in-session UNDO TOAST**, NOT a "recently deleted"
  view. The api list is non-deleted-only and there is **no "list deleted"
  endpoint** (adding one is an api change, out of scope for this phase). So a
  successful delete raises an undo affordance (`<output>` = implicit
  `role="status"`, a polite live region — never `role="alert"`, which the App
  Router route announcer already owns) that `POST`s `/api/documents/[id]/restore`
  (body-less; the full doc comes back with content intact). The toast holds the
  **last** delete only; a new delete replaces it, and a reload/navigation clears
  it. That is by design: the toast is the in-session undo window, and the
  confirm prompt is the guard against accidental loss. A failed restore **keeps**
  the toast so the user can retry — it never double-clobbers.
- **Single mutation at a time** (`busyRef`): the in-flight id gates its row's
  Delete and the toast's Undo so a second click never races the first.
- All ids are `encodeURIComponent`'d into the proxy URLs; previews stay PLAIN
  TEXT (`preview` field) — **zero `dangerouslySetInnerHTML`** (repo invariant).

## Integrations — the OAuth vault web surface (Phase 44, B3)

`/settings/integrations` lets a user connect a Google account so finished
sermons can later be pushed into Drive (the pull/push itself is Phase 45). The
web layer is **purely HTTP** — it never sees a token. Locked decisions:

- **The JWT/bearer NEVER reaches the browser, and NO token material ever does.**
  Only `provider`, `provider_account_email` (the one token-derived value the API
  returns), `scopes`, and timestamps cross the wire
  (`lib/types.ts:IntegrationConnection` — there are **no** token/ciphertext
  fields). The refresh/access tokens are encrypted at rest on the API and never
  leave it.
- **Provider allow-set is the gate.** `lib/integrations.ts:ALLOWED_PROVIDERS`
  (`google` now; `microsoft` is Phase 46, config-only there). Every proxy
  validates the `{provider}` path param against it and **404s an unknown
  provider BEFORE attaching the bearer / calling the API** — a probe for
  `/api/integrations/evil/...` never reaches the API.
- **Four same-origin route handlers** (the Phase 15/16 proxy pattern, bearer
  from the HttpOnly cookie server-side):
  - `GET /api/integrations` — list (`app/api/integrations/route.ts`).
  - `POST /api/integrations/[provider]/authorize` — kickoff. Asks the API to
    mint the state HMAC + PKCE challenge (the verifier is stored server-side in
    Redis, **never** in the browser) and returns `{authorize_url}`. **Nothing is
    read from the request body** — provider comes from the path allow-set only.
  - `GET /api/integrations/[provider]/callback` — the **PUBLIC, operator-
    registered redirect URI** (`/api/integrations/google/callback` on the web
    origin, ports 3000 AND 3001). The provider top-level-redirects the browser
    here with `?code&state`; the SameSite=Lax `sg_session` cookie rides along on
    that top-level GET, so `getSessionToken()` works. The handler forwards
    `code`+`state` to the API callback **server-side** with the bearer (the full
    state-HMAC + PKCE + account-binding CSRF validation runs on the API before
    any token exchange), then **302s the browser to a FIXED same-origin path**:
    `/settings/integrations?connected={provider}` on success or `?error={code}`
    on failure. The redirect target is a constant path with only a vetted
    provider / short generic error code interpolated — **no open redirect**, and
    the API's error detail is **never echoed verbatim** (the API stays the only
    oracle). `code`/`state` are **never logged**.
  - `DELETE /api/integrations/[provider]` — revoke. The uniform 404
    (not-connected / cross-tenant) passes through byte-for-byte (no existence
    oracle).
- **`sameOriginUrl(req, path)` in the callback** derives the redirect base from
  the forwarded `host` header, **not `req.url`**. The dev server normalizes
  `req.url`'s host to `localhost`, which would land the redirect on a *different*
  cookie origin than `127.0.0.1` (distinct origins) and silently bounce the user
  to `/login`. The host header only ever builds a same-origin redirect to a
  fixed path — never reflected into a body — so it is not an injection vector.
- **Page** (`app/settings/integrations/page.tsx`) is a **server component**:
  fetches the connections via `lib/api-server.ts:getIntegrations()` (mirrors
  `getDocuments` — bearer stays on the server) and renders the `IntegrationsPanel`
  **client island** (Connect/Disconnect — a server component can't mutate or set
  `window.location`). Connect POSTs the authorize proxy then
  `window.location.assign(authorize_url)` (a TOP-LEVEL nav so the Lax cookie
  survives). Disconnect is **confirm-gated** (re-consent is needed to reconnect).
  The `?connected`/`?error` banner inputs are re-validated/mapped against fixed
  sets — never echoed as free text.
- **`middleware.ts` matcher** gained `/settings/:path*` (auth-gated). The
  callback under `/api/*` is **not** matched by the page matcher (which lists
  page paths) and relies on the cookie it carries.
- **E2E** (`e2e/integrations.spec.ts`): the fake api (`e2e/support/fake-api.mjs`)
  gained `/integrations` (list), `/integrations/{provider}/authorize` (mints a
  one-shot account-bound state, returns an authorize_url at a stub
  `/oauth/consent`), the stub consent screen (302s the browser back to the web
  callback at `E2E_WEB_ORIGIN` with a deterministic `code`+`state` — standing in
  for Google's top-level redirect, so **no real Google round-trip**),
  `/integrations/{provider}/callback` (single-use + account-binding state check
  before "exchange", then stores only the account email/scopes — **never a
  token**), and `DELETE /integrations/{provider}` (uniform 404). The spec drives
  Connect → consent → callback → connected, then Disconnect.

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
  and the `/documents` endpoints (POST create → 201 full doc, GET list →
  preview-only items with no `content` key, GET/`{id}` full, PATCH/`{id}` → 200 /
  **409 on a stale `base_updated_at`**, DELETE soft, and the Phase-36
  POST/`{id}/restore` → 200 full doc with content intact) — all bearer-scoped
  with the same uniform 404 for non-owned/unknown ids. A strictly-monotonic `updated_at`
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
