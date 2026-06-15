import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright E2E for the Next.js frontend (Phase 25).
 *
 * Two `webServer` entries boot together before the suite:
 *  1. the fake api (`e2e/support/fake-api.mjs`) on FAKE_API_PORT — an in-memory
 *     stand-in that speaks the real api's wire contracts (auth, search-summary
 *     with resolving citation markers, upload, the Phase-20 ownership-404). The
 *     web CI job has no Postgres/Milvus/worker services, so this is the
 *     documented CI boundary (web/AGENTS.md); the live/nightly path swaps in the
 *     real api + e2e/support/stub-llm.mjs via SERMON_API_LLM_BASE_URL.
 *  2. `next dev` on E2E_WEB_PORT (3100, NEVER 3000 — the :3000 conflict on the
 *     dev box is real), with API_BASE_URL pointed at the fake api so the
 *     same-origin /api/* proxies reach it.
 *
 * Ports are overridable via env for parallel local runs / the live path.
 */

const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 3100);
const FAKE_API_PORT = Number(process.env.FAKE_API_PORT ?? 8081);
// When E2E_API_BASE_URL is set (live/nightly path: real api + stub-llm) we do
// NOT boot the fake api and point the web server at the provided base instead.
const EXTERNAL_API = process.env.E2E_API_BASE_URL;
const apiBaseUrl = EXTERNAL_API ?? `http://127.0.0.1:${FAKE_API_PORT}`;

const fakeApiServer = {
  command: "node e2e/support/fake-api.mjs",
  port: FAKE_API_PORT,
  reuseExistingServer: !process.env.CI,
  stdout: "pipe" as const,
  stderr: "pipe" as const,
  env: { PORT: String(FAKE_API_PORT) },
};

const webServer = {
  command: `next dev --port ${WEB_PORT}`,
  port: WEB_PORT,
  reuseExistingServer: !process.env.CI,
  // `next dev`'s first boot (npm/uv-free, but Next has to spin up its compiler)
  // is slow on a cold CI runner — give it generous headroom before Playwright
  // gives up waiting for the port. Separate from per-route on-demand compiles,
  // which the navigationTimeout below absorbs.
  timeout: 120_000,
  stdout: "pipe" as const,
  stderr: "pipe" as const,
  // API_BASE_URL is server-only (lib/config.ts) — the browser never sees it.
  env: { PORT: String(WEB_PORT), API_BASE_URL: apiBaseUrl },
};

export default defineConfig({
  testDir: "./e2e",
  // Only the spec files are tests — the support/ servers are plain scripts.
  testMatch: "**/*.spec.ts",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  // Single worker in CI (one fake-api + one dev server) — omit the key locally
  // so Playwright picks its default. exactOptionalPropertyTypes forbids an
  // explicit `undefined`, so spread it in conditionally.
  //
  // workers:1 in CI is the COLD-START fix, not just a resource limit: with
  // parallel workers each test hits a never-compiled `next dev` route at the
  // same instant, stacking N simultaneous on-demand compiles past the 30s
  // default and flaking the first cold run. Serializing makes each route
  // compile once, sequentially; the first hit pays the compile, the rest are
  // warm. Retries stay as a backstop, not the primary fix.
  ...(process.env.CI ? { workers: 1 } : {}),
  // A single cold on-demand route compile on a slow runner can exceed the 30s
  // default navigation timeout, so raise the page-navigation and action waits
  // enough to absorb one compile. Also lift the per-test cap (default 30s) so a
  // navigate-then-act test that pays a cold compile doesn't trip the test
  // timeout either.
  timeout: 90_000,
  reporter: process.env.CI ? [["github"], ["list"]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    navigationTimeout: 60_000,
    actionTimeout: 15_000,
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  // Boot the fake api first (web depends on it via API_BASE_URL), then web.
  // Skip the fake-api server when an external api is supplied (live path).
  webServer: EXTERNAL_API ? [webServer] : [fakeApiServer, webServer],
});
