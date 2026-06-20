import { expect, test } from "@playwright/test";
import { loginViaUi, makeUser, signUp } from "./support/users";

/**
 * OAuth integrations vault — connect / list / disconnect (Phase 44). Drives the
 * /settings/integrations page against the same-origin proxies
 * (POST /api/integrations/google/authorize, the public
 * GET /api/integrations/google/callback redirect URI, DELETE
 * /api/integrations/google), backed by the in-memory fake api
 * (e2e/support/fake-api.mjs integrations endpoints + a stub consent screen) in
 * CI and the real api on the nightly live path.
 *
 * The stub consent screen stands in for Google's accounts.google.com: the
 * authorize proxy returns an authorize_url at the fake api's /oauth/consent,
 * which 302s the browser back to the web callback with a deterministic
 * code+state — so the whole connect round-trip runs with no real Google.
 *
 * What it pins:
 *   1. an UNAUTHENTICATED /settings/integrations bounces to /login (the
 *      middleware matcher now covers /settings);
 *   2. Connect -> consent -> callback -> the page shows the connected account
 *      email (the only token-derived value ever surfaced);
 *   3. Disconnect (confirm-gated) removes the connection and returns to the
 *      not-connected state;
 *   4. the session JWT is NEVER exposed to the browser across the flow.
 */

test("unauthenticated /settings/integrations redirects to /login", async ({ page }) => {
  await page.goto("/settings/integrations");
  await page.waitForURL(/\/login\?/);
  expect(page.url()).toContain("next=%2Fsettings%2Fintegrations");
  await expect(page.getByRole("button", { name: "Log in" })).toBeVisible();
});

test("connect -> consent -> callback shows the connected account, then disconnect removes it", async ({
  page,
}) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/settings/integrations");

  // Not connected yet — the Connect button is shown.
  const connectBtn = page.getByRole("button", { name: "Connect Google Drive" });
  await expect(connectBtn).toBeVisible();

  // Connect: the button POSTs the authorize proxy, gets the authorize_url, and
  // window.location.assign()s to the stub consent screen, which 302s back to the
  // web callback, which 302s back to /settings/integrations?connected=google.
  await connectBtn.click();
  await page.waitForURL(/\/settings\/integrations\?connected=google/);

  // The success banner + the connected account email render (the account email
  // is the only token-derived value the API ever returns to the browser).
  await expect(page.getByRole("status")).toContainText("Connected to Google Drive");
  await expect(page.getByText(/Connected as oauth-.*@example\.com/)).toBeVisible();

  // The session JWT never reaches the browser (HttpOnly cookie, server-only
  // bearer). Nothing token-shaped is in the rendered HTML.
  const html = await page.content();
  expect(html).not.toMatch(/refresh_token|access_token|Bearer /);

  // Disconnect is confirm-gated; accept and the row returns to not-connected.
  const disconnectBtn = page.getByRole("button", { name: "Disconnect Google Drive" });
  await expect(disconnectBtn).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await disconnectBtn.click();

  await expect(page.getByRole("button", { name: "Connect Google Drive" })).toBeVisible();
  await expect(page.getByText(/Connected as/)).toHaveCount(0);
});

test("cancelling the disconnect confirm keeps the connection", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/settings/integrations");

  await page.getByRole("button", { name: "Connect Google Drive" }).click();
  await page.waitForURL(/\/settings\/integrations\?connected=google/);

  const disconnectBtn = page.getByRole("button", { name: "Disconnect Google Drive" });
  await expect(disconnectBtn).toBeVisible();

  // Dismiss the confirm — nothing is disconnected.
  page.once("dialog", (dialog) => dialog.dismiss());
  await disconnectBtn.click();
  await expect(disconnectBtn).toBeVisible();
});
