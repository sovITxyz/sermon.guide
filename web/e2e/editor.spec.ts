import { expect, test } from "@playwright/test";
import { loginViaUi, makeUser, signUp } from "./support/users";

/**
 * Editor smoke (Phase 35 slice B; migrated to AUTOSAVE in Phase 36 slice C).
 * Drives the things cookie-jar round-trips cannot type, and now asserts on the
 * autosave loop — there is NO Save button anymore (Phase 36 removed it):
 *   1. login -> /sermons -> "New sermon" -> the editor opens on the new doc;
 *   2. type real text into the TipTap contenteditable, then WAIT for the
 *      save-status indicator to settle on "saved" (the autosave PATCH landed) —
 *      driven off the SaveIndicator's `data-save-status` hook, never a click;
 *   3. reload -> the typed content persists (round-tripped through the proxy +
 *      api as ProseMirror JSON);
 *   4. CONFLICT UX: two tabs on the same doc; the second tab autosaves first and
 *      bumps updated_at, so the first tab's base_updated_at is now stale. The
 *      first tab's autosave fires a 409 -> the inline conflict banner appears
 *      (non-destructive: the first tab's buffer is untouched), and "Reload
 *      latest" recovers the first tab to the second tab's content.
 *
 * Backend is the in-memory fake api in CI (e2e/support/fake-api.mjs, documents
 * endpoints, with a strictly-monotonic updated_at so the 409 is deterministic)
 * and the real api on the nightly live path.
 */

const SERMON_TEXT = "The grace of God appears in this manuscript.";

/** The TipTap/ProseMirror editing surface. */
function editorBody(page: import("@playwright/test").Page) {
  return page.locator('[contenteditable="true"]');
}

/** The SaveIndicator's machine-readable status hook (saved/saving/unsaved/
 * error/conflict). Asserting on the attribute, not the human label, keeps the
 * waits decoupled from copy. */
function saveStatus(page: import("@playwright/test").Page) {
  return page.locator("[data-save-status]");
}

/**
 * Type into the editor and wait for autosave to fully settle on "saved".
 *
 * The indicator starts at "saved" on mount, so we can't just wait for "saved"
 * (that's already true) — we first wait for the edit to flip it OFF "saved"
 * (the synchronous "unsaved" the scheduler sets on every keystroke), THEN wait
 * for it to return to "saved" once the debounced PATCH resolves. The timeout is
 * tolerant of the 2 s debounce + the PATCH round-trip, but it waits on the
 * status SIGNAL rather than sleeping blindly.
 */
async function typeAndAwaitSaved(
  page: import("@playwright/test").Page,
  text: string,
): Promise<void> {
  const body = editorBody(page);
  await expect(body).toBeVisible();
  await body.click();
  await body.fill(text);
  // The keystroke synchronously marks the buffer dirty…
  await expect(saveStatus(page)).not.toHaveAttribute("data-save-status", "saved");
  // …and the debounced autosave PATCH then settles it back to "saved".
  await expect(saveStatus(page)).toHaveAttribute("data-save-status", "saved", { timeout: 15_000 });
}

test("create -> type -> autosave persists the content across reload", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/sermons");

  // Create a sermon: the button POSTs through /api/documents then routes to the
  // editor at /sermons/[newId].
  await page.getByRole("button", { name: "New sermon" }).click();
  await page.waitForURL(/\/sermons\/[^/]+$/);

  // The editor (dynamic-imported) mounts; type and let AUTOSAVE persist — no
  // Save button to click anymore.
  await typeAndAwaitSaved(page, SERMON_TEXT);

  // Reload: the server shell re-fetches the doc, the editor opens with the
  // persisted content.
  await page.reload();
  await expect(editorBody(page)).toContainText(SERMON_TEXT);
});

test("a stale-tab autosave surfaces the 409 conflict and reloads to latest", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/sermons");

  await page.getByRole("button", { name: "New sermon" }).click();
  await page.waitForURL(/\/sermons\/[^/]+$/);
  const editorUrl = page.url();

  // First autosave from this tab establishes a known updated_at (tab1 adopts it
  // as its base on the 200).
  await typeAndAwaitSaved(page, "First edit.");

  // A SECOND tab (same session) opens the same editor — it GETs the current
  // updated_at as ITS base.
  const otherTab = await page.context().newPage();
  try {
    await otherTab.goto(editorUrl);
    const SECOND_TAB_TEXT = "Second tab edit, the latest version.";
    // Tab2 autosaves -> the monotonic updated_at advances past tab1's base.
    await typeAndAwaitSaved(otherTab, SECOND_TAB_TEXT);

    // Back in the FIRST tab: its base_updated_at is now stale (tab2 bumped it).
    // Its autosave PATCH -> 409 -> the loop STOPS and the conflict banner shows.
    // The buffer is NOT clobbered, so the status leaves "saved" and never
    // returns there (it lands on "conflict").
    const body = editorBody(page);
    await body.click();
    await body.fill("First tab, stale edit.");
    await expect(saveStatus(page)).toHaveAttribute("data-save-status", "conflict", {
      timeout: 15_000,
    });

    // Scope to SermonEditor's inline conflict banner: a bare getByRole("alert")
    // also matches Next.js's always-present #__next-route-announcer__
    // (strict-mode violation — web/AGENTS.md), so assert on the banner's copy.
    const conflictBanner = page.getByText(
      "This sermon was changed in another tab or device since you opened it.",
    );
    await expect(conflictBanner).toBeVisible();
    // Non-destructive: the first tab's edits are still in the buffer.
    await expect(body).toContainText("First tab, stale edit.");

    // Recover: "Reload latest" re-GETs the doc, resets the editor to tab2's
    // content + a fresh base, and resumes autosave.
    await page.getByRole("button", { name: "Reload latest" }).click();
    await expect(saveStatus(page)).toHaveAttribute("data-save-status", "saved", {
      timeout: 15_000,
    });
    await expect(body).toContainText(SECOND_TAB_TEXT);
    await expect(body).not.toContainText("First tab, stale edit.");
    // The banner is gone once recovered.
    await expect(conflictBanner).toHaveCount(0);
  } finally {
    await otherTab.close();
  }
});
