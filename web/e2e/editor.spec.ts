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
 *   5. CITATION DRAWER (Phase 37): open the in-editor LibraryDrawer, search the
 *      library through /api/search (RAW hits, no LLM wait), click a hit to insert
 *      a `citation` block (cached title + snippet + a Read-in-context deep link),
 *      let autosave settle, and reload — the node parses on load and persists
 *      through documents.content JSON.
 *
 * Backend is the in-memory fake api in CI (e2e/support/fake-api.mjs: documents
 * endpoints with a strictly-monotonic updated_at so the 409 is deterministic,
 * plus the Phase-37 /search raw hits + /library owned set) and the real api on
 * the nightly live path.
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

test("drawer search inserts a citation block that persists across reload", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/sermons");

  await page.getByRole("button", { name: "New sermon" }).click();
  await page.waitForURL(/\/sermons\/[^/]+$/);

  // The editor (dynamic-imported) mounts; type a little so the doc is non-empty,
  // then let autosave settle before opening the drawer.
  await typeAndAwaitSaved(page, "A sermon about grace.");

  // Open the in-editor LibraryDrawer from the toolbar affordance.
  await page.getByRole("button", { name: "Cite from your library" }).click();

  // Search the library through the NEW /api/search proxy (raw hits, NO LLM wait —
  // the drawer settles fast, no minutes-long ticker). The fake api returns
  // deterministic hits whose book_ids are in the user's /library.
  await page.getByLabel("Search your library").fill("grace");
  await page.getByRole("button", { name: "Search" }).click();

  // A hit row renders the title (from the one-shot /library map) + the snippet
  // (the raw content_chunk) — clicking it inserts the citation node.
  const hitRow = page.getByTestId("library-drawer-hit").first();
  await expect(hitRow).toContainText("On Grace");
  await hitRow.click();

  // The inserted citation block shows the cached title + snippet and, because the
  // book is in the library, a "Read in context" deep-link to the cited chunk.
  const card = page.locator('[data-type="citation"]');
  await expect(card).toContainText("On Grace");
  await expect(card).toContainText("Grace is the unearned favor of God");
  await expect(page.getByTestId("citation-read-link")).toHaveAttribute(
    "href",
    "/read/11111111-1111-1111-1111-111111111111?chunk=3",
  );

  // The insert fired the editor `update` -> autosave persists it; wait for saved.
  await expect(saveStatus(page)).toHaveAttribute("data-save-status", "saved", { timeout: 15_000 });

  // Reload: the server shell re-fetches the doc; the stored citation node parses
  // on load and re-renders from its cached attrs — the block survives the round
  // trip through documents.content JSON.
  await page.reload();
  const reloaded = page.locator('[data-type="citation"]');
  await expect(reloaded).toContainText("On Grace");
  await expect(reloaded).toContainText("Grace is the unearned favor of God");
});

test("export downloads a .docx and import replaces the editor content", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/sermons");

  await page.getByRole("button", { name: "New sermon" }).click();
  await page.waitForURL(/\/sermons\/[^/]+$/);

  // Seed some content so the doc is non-empty before the round-trip.
  await typeAndAwaitSaved(page, "Original manuscript text.");

  // Download .docx: the button fetches the export proxy as a blob and triggers a
  // browser download. Capture the Playwright download event and assert the
  // suggested filename came from the API's sanitized Content-Disposition.
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download as Word document" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/\.docx$/);

  // Import .docx: drive the hidden file input directly (the Uploader pattern).
  // The fake api overwrites content with a deterministic imported doc and
  // returns the full document; the editor reloads it as TipTap JSON.
  await page.locator('input[aria-label="Word document to import"]').setInputFiles({
    name: "import.docx",
    mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    buffer: Buffer.from("PK fake docx bytes"),
  });

  // The editor now shows the imported content, and the prior text is gone.
  await expect(editorBody(page)).toContainText("Imported from a Word document.");
  await expect(editorBody(page)).not.toContainText("Original manuscript text.");
});

test("a rejected import surfaces a visible error and leaves the editor intact", async ({
  page,
}) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/sermons");

  await page.getByRole("button", { name: "New sermon" }).click();
  await page.waitForURL(/\/sermons\/[^/]+$/);

  await typeAndAwaitSaved(page, "Keep this manuscript.");

  // The fake api 415s a file named reject.docx (mirrors the API's libmagic
  // sniff refusing a non-docx) — the proxy passes the 4xx through and the editor
  // shows the visible error banner WITHOUT clobbering the buffer.
  await page.locator('input[aria-label="Word document to import"]').setInputFiles({
    name: "reject.docx",
    mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    buffer: Buffer.from("not really a docx"),
  });

  await expect(page.getByTestId("docx-error")).toBeVisible();
  // The buffer is untouched by the rejected import.
  await expect(editorBody(page)).toContainText("Keep this manuscript.");

  // Dismiss clears the error.
  await page.getByRole("button", { name: "Dismiss error" }).click();
  await expect(page.getByTestId("docx-error")).toHaveCount(0);
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
