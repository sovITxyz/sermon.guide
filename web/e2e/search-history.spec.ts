import { expect, test } from "@playwright/test";
import { loginViaUi, makeUser, signUp } from "./support/users";

/** Escape a string for safe use inside a RegExp (the question has a `?`). */
function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Search history (Phase 51): run a summary search -> it is saved and appears in
 * the "Recent" panel -> reopening it renders the saved summary INSTANTLY with NO
 * second /search-summary call (the costly pipeline runs once, replays are free)
 * -> deleting it removes it from the panel.
 *
 * The fake api saves a history row on every successful /search-summary and serves
 * the lightweight list + the full per-id replay blob; the page server-fetches the
 * list and the SearchWorkspace refreshes it after a live search.
 */
test("a summary search is saved to Recent, reopens without re-running, then deletes", async ({
  page,
}) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/search");

  // Count every /search-summary POST: the reopen must add ZERO to this.
  let summaryCalls = 0;
  page.on("request", (req) => {
    if (req.method() === "POST" && req.url().endsWith("/api/search-summary")) {
      summaryCalls += 1;
    }
  });

  const recent = page.getByTestId("recent-searches");
  await expect(recent.getByText(/No recent searches yet/i)).toBeVisible();

  // Run a search -> the grounded Summary card renders.
  const question = "How do grace and faith relate?";
  await page.getByLabel("Question").fill(question);
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page.getByRole("heading", { name: "Summary" })).toBeVisible();
  expect(summaryCalls).toBe(1);

  // The search was saved; the panel refreshes and the row appears (its Delete
  // affordance carries the exact query in its aria-label).
  const deleteRow = recent.getByRole("button", { name: `Delete recent search: ${question}` });
  await expect(deleteRow).toBeVisible();

  // Reopen the saved search: the row-open button is the row's first button. The
  // click must fetch the full entry (GET /api/search-history/{id}) and NOT
  // re-run /search-summary.
  const historyGet = page.waitForRequest(
    (req) => req.method() === "GET" && /\/api\/search-history\/[^/]+$/.test(req.url()),
  );
  // The row-open button's accessible name STARTS WITH the query (the Delete
  // button's starts with "Delete recent search:"), so an anchored-regex match is
  // unambiguous — no need to filter the <li> by the delete button.
  const openRow = recent.getByRole("button", {
    name: new RegExp(`^${escapeRegExp(question)}`),
  });
  await openRow.click();
  await historyGet;

  // The saved summary still renders, and no second summary call was made.
  await expect(page.getByRole("heading", { name: "Summary" })).toBeVisible();
  expect(summaryCalls).toBe(1);

  // Delete the saved search -> it disappears from the Recent panel.
  await deleteRow.click();
  await expect(recent.getByText(/No recent searches yet/i)).toBeVisible();
});
