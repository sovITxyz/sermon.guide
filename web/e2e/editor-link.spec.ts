import { type Page, expect, test } from "@playwright/test";
import { loginViaUi, makeUser, signUp } from "./support/users";

/**
 * External-editor link (Google Docs) — Phase 45, B4. Drives the read-only lock
 * end to end against the same-origin proxies
 * (POST /api/documents/{id}/editor-link, GET .../status, POST .../pull,
 * POST .../unlink), backed by the in-memory fake api (e2e/support/fake-api.mjs
 * editor-link endpoints) in CI and the real api on the nightly live path.
 *
 * What it pins (the make-or-break read-only-lock UX):
 *   1. connect Google -> create a sermon -> the "Link to Google Docs" button is
 *      shown (a connection exists);
 *   2. Link -> the editor flips to HARD read-only: the formatting toolbar is
 *      gone, the contenteditable is no longer editable, and the "Editing
 *      externally in Google Docs" banner shows Open / Pull / Unlink. Open is an
 *      anchor to the Drive web_url with rel="noopener noreferrer" (the only
 *      external string; NO token in the page);
 *   3. Pull -> the editor content updates to the pulled Doc content (reloaded as
 *      TipTap JSON, not injected HTML);
 *   4. Unlink (keep-app via the settled choice dialog) -> the editor returns to
 *      editable, the banner is gone, and the toolbar is back.
 *
 * No real Google round-trip: the connect flow uses the stub consent screen
 * (integrations.spec pattern) and the link/pull/unlink endpoints are the fake
 * api's deterministic stand-ins.
 */

/** The TipTap editing surface while EDITABLE (contenteditable="true"). */
function editorBody(page: Page) {
  return page.locator('[contenteditable="true"]');
}

/** The TipTap surface regardless of editable state (ProseMirror always present),
 * so a read-only ("contenteditable=false") assertion can still find it. */
function proseMirror(page: Page) {
  return page.locator(".ProseMirror");
}

/** Connect a Google account via the stub consent flow (integrations.spec). */
async function connectGoogle(page: Page): Promise<void> {
  await page.goto("/settings/integrations");
  await page.getByRole("button", { name: "Connect Google Drive" }).click();
  await page.waitForURL(/\/settings\/integrations\?connected=google/);
  await expect(page.getByText(/Connected as oauth-.*@example\.com/)).toBeVisible();
}

test("link locks the editor read-only, pull updates content, unlink restores editing", async ({
  page,
}) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/settings/integrations");

  // 1. A Google connection must exist for the Link affordance to appear.
  await connectGoogle(page);

  // Create a sermon and seed some content.
  await page.goto("/sermons");
  await page.getByRole("button", { name: "New sermon" }).click();
  await page.waitForURL(/\/sermons\/[^/]+$/);

  const body = editorBody(page);
  await expect(body).toBeVisible();
  await body.click();
  await body.fill("My original manuscript.");
  // Let autosave settle before linking.
  await expect(page.locator("[data-save-status]")).toHaveAttribute("data-save-status", "saved", {
    timeout: 15_000,
  });

  // 2. Link to Google Docs -> the editor flips to read-only with the banner.
  const linkBtn = page.getByRole("button", { name: "Link to Google Docs" });
  await expect(linkBtn).toBeVisible();
  await linkBtn.click();

  const banner = page.getByTestId("editing-externally-banner");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("Editing externally in Google Docs");

  // The contenteditable is no longer editable and the toolbar is gone.
  await expect(proseMirror(page)).toHaveAttribute("contenteditable", "false");
  await expect(page.getByRole("button", { name: "Bold" })).toHaveCount(0);

  // Open is an anchor to the Drive web_url, opened safely.
  const openLink = page.getByRole("link", { name: "Open in Google Docs" });
  await expect(openLink).toHaveAttribute("href", /^https:\/\/docs\.google\.com\/document\/d\//);
  await expect(openLink).toHaveAttribute("rel", "noopener noreferrer");
  await expect(openLink).toHaveAttribute("target", "_blank");

  // No token material is anywhere in the page.
  const html = await page.content();
  expect(html).not.toMatch(/refresh_token|access_token|Bearer /);

  // 3. Pull changes -> the editor content updates to the pulled Doc content.
  await page.getByRole("button", { name: "Pull changes" }).click();
  await expect(proseMirror(page)).toContainText("Pulled from Google Docs.");
  // Still read-only after a pull.
  await expect(proseMirror(page)).toHaveAttribute("contenteditable", "false");

  // 4. Unlink -> the settled choice dialog offers both modes; keep-app returns
  // the editor to editable with the toolbar back and the banner gone.
  await page.getByRole("button", { name: "Unlink" }).click();
  const dialog = page.getByTestId("unlink-dialog");
  await expect(dialog).toBeVisible();
  await expect(page.getByRole("button", { name: "Pull final copy & unlink" })).toBeVisible();
  await page.getByRole("button", { name: "Keep this version & unlink" }).click();

  await expect(page.getByTestId("editing-externally-banner")).toHaveCount(0);
  await expect(editorBody(page)).toHaveAttribute("contenteditable", "true");
  await expect(page.getByRole("button", { name: "Bold" })).toBeVisible();
  // keep-app left the pulled content in place (it was already pulled), still shown.
  await expect(editorBody(page)).toContainText("Pulled from Google Docs.");
});
