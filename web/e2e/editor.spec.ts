import { expect, test } from "@playwright/test";
import { loginViaUi, makeUser, signUp } from "./support/users";

/**
 * Editor smoke (Phase 35, B2 slice B). Drives the things cookie-jar round-trips
 * cannot type:
 *   1. login -> /sermons -> "New sermon" -> the editor opens on the new doc;
 *   2. type real text into the TipTap contenteditable, click Save -> "Saved";
 *   3. reload -> the typed content persists (round-tripped through the proxy +
 *      api as ProseMirror JSON);
 *   4. a STALE-TAB save surfaces the 409 as a non-destructive inline error
 *      (a second tab saved first, bumping updated_at; the first tab's
 *      base_updated_at is now stale). Full conflict UX is Phase 36.
 *
 * Backend is the in-memory fake api in CI (e2e/support/fake-api.mjs, documents
 * endpoints) and the real api on the nightly live path.
 */

const SERMON_TEXT = "The grace of God appears in this manuscript.";

/** The TipTap/ProseMirror editing surface. */
function editorBody(page: import("@playwright/test").Page) {
  return page.locator('[contenteditable="true"]');
}

test("create -> type -> Save -> reload persists the content", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/sermons");

  // Create a sermon: the button POSTs through /api/documents then routes to the
  // editor at /sermons/[newId].
  await page.getByRole("button", { name: "New sermon" }).click();
  await page.waitForURL(/\/sermons\/[^/]+$/);

  // The editor (dynamic-imported) mounts; type into the contenteditable.
  const body = editorBody(page);
  await expect(body).toBeVisible();
  await body.click();
  await body.fill(SERMON_TEXT);

  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Saved")).toBeVisible();

  // Reload: the server shell re-fetches the doc, the editor opens with the
  // persisted content.
  await page.reload();
  await expect(editorBody(page)).toContainText(SERMON_TEXT);
});

test("a stale-tab Save surfaces the 409 conflict as a non-destructive error", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/sermons");

  await page.getByRole("button", { name: "New sermon" }).click();
  await page.waitForURL(/\/sermons\/[^/]+$/);
  const editorUrl = page.url();

  // First save from this tab establishes a known updated_at.
  await editorBody(page).click();
  await editorBody(page).fill("First edit.");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Saved")).toBeVisible();

  // A SECOND tab (same session) opens the same editor — it loads the current
  // updated_at as its base.
  const otherTab = await page.context().newPage();
  try {
    await otherTab.goto(editorUrl);
    await expect(editorBody(otherTab)).toBeVisible();
    await editorBody(otherTab).click();
    await editorBody(otherTab).fill("Second tab edit.");
    await otherTab.getByRole("button", { name: "Save" }).click();
    await expect(otherTab.getByText("Saved")).toBeVisible();

    // Back in the FIRST tab: its base_updated_at is now stale (the second tab
    // bumped it). Saving again -> 409 -> a non-destructive inline error; the
    // first tab's buffer is untouched.
    await editorBody(page).click();
    await editorBody(page).fill("First tab, stale edit.");
    await page.getByRole("button", { name: "Save" }).click();

    // Scope to SermonEditor's inline 409 copy: a bare getByRole("alert") also
    // matches Next.js's always-present #__next-route-announcer__ (strict-mode
    // violation), so assert on the conflict message text directly.
    await expect(
      page.getByText("This sermon was changed in another tab or device since you opened it."),
    ).toBeVisible();
    await expect(editorBody(page)).toContainText("First tab, stale edit.");
  } finally {
    await otherTab.close();
  }
});
