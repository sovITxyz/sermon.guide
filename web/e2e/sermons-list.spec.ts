import { expect, test } from "@playwright/test";
import { loginViaUi, makeUser, signUp } from "./support/users";

/**
 * Sermons-list delete/restore (Phase 36, B2 slice C). Drives the row actions on
 * /sermons against the same-origin DELETE /api/documents/[id] and
 * POST /api/documents/[id]/restore proxies:
 *   1. create a sermon, return to the list, and confirm it is listed;
 *   2. delete it (confirm-gated) -> it vanishes from the list, and its editor
 *      route now 404s (the api list is non-deleted only; the soft-deleted doc is
 *      invisible);
 *   3. UNDO from the toast -> the sermon returns to the list with its title
 *      intact (restore clears deleted_at, content untouched).
 *
 * Restore reachability is the in-session undo toast (web/AGENTS.md) — there is
 * no "recently deleted" view, so the undo must be exercised before any reload.
 *
 * Backend is the in-memory fake api in CI (e2e/support/fake-api.mjs, documents
 * delete/restore endpoints) and the real api on the nightly live path.
 */

const SERMON_TEXT = "The list-action manuscript, written to be deleted and restored.";

function editorBody(page: import("@playwright/test").Page) {
  return page.locator('[contenteditable="true"]');
}

/** Create one sermon via the New-sermon flow, type text, and let autosave
 * persist it; then return to the list. Returns the sermon's editor URL. */
async function createSermon(page: import("@playwright/test").Page): Promise<string> {
  await page.getByRole("button", { name: "New sermon" }).click();
  await page.waitForURL(/\/sermons\/[^/]+$/);
  const editorUrl = page.url();

  const body = editorBody(page);
  await expect(body).toBeVisible();
  await body.click();
  await body.fill(SERMON_TEXT);
  // Autosave persists on its own; wait for the indicator to settle to "Saved".
  await expect(page.getByText("Saved")).toBeVisible();

  await page.goto("/sermons");
  return editorUrl;
}

test("delete removes the sermon from the list and 404s its editor route", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/sermons");

  const editorUrl = await createSermon(page);

  // The sermon is listed with a delete action.
  const deleteBtn = page.getByRole("button", { name: /^Delete/ });
  await expect(deleteBtn).toBeVisible();

  // Confirm-before-delete: accept the dialog (a manuscript is irreplaceable).
  page.once("dialog", (dialog) => dialog.accept());
  await deleteBtn.click();

  // Gone from the list -> the empty state shows (this user's only sermon).
  await expect(page.getByText(/no sermons yet/i)).toBeVisible();

  // The soft-deleted doc is invisible: its editor route renders the not-found
  // state (the api returns the uniform 404 for a deleted doc).
  await page.goto(editorUrl);
  await expect(page.getByText(/not found/i)).toBeVisible();
});

test("cancelling the delete confirm keeps the sermon", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/sermons");

  await createSermon(page);
  const deleteBtn = page.getByRole("button", { name: /^Delete/ });
  await expect(deleteBtn).toBeVisible();

  // Dismiss the confirm — nothing is deleted.
  page.once("dialog", (dialog) => dialog.dismiss());
  await deleteBtn.click();

  // Still listed; no undo toast was raised.
  await expect(deleteBtn).toBeVisible();
  await expect(page.getByRole("button", { name: "Undo" })).toHaveCount(0);
});

test("undo restores a just-deleted sermon to the list", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/sermons");

  await createSermon(page);

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: /^Delete/ }).click();

  // The undo toast appears; restore the sermon.
  const undoBtn = page.getByRole("button", { name: "Undo" });
  await expect(undoBtn).toBeVisible();
  await undoBtn.click();

  // Back in the list with its delete action — and openable with content intact.
  const deleteBtn = page.getByRole("button", { name: /^Delete/ });
  await expect(deleteBtn).toBeVisible();
  await expect(page.getByText(/no sermons yet/i)).toHaveCount(0);

  await page.getByRole("link", { name: /list-action manuscript/i }).click();
  await page.waitForURL(/\/sermons\/[^/]+$/);
  await expect(editorBody(page)).toContainText(SERMON_TEXT);
});
