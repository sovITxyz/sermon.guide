import { expect, test } from "@playwright/test";
import { loginViaUi, makeUser, signUp } from "./support/users";

/**
 * Login -> search -> grounded summary with resolving citation chips.
 *
 * The summary comes back deterministic (fake api in CI, real api + stub-llm on
 * the live path) with [book:chunk] markers that exactly match the returned
 * citations, so the Phase 24 chip renderer must explode them into linked chips
 * ([1], [2]) that anchor to their source cards (#citation-1, #citation-2).
 */
test("login then search renders a grounded summary with resolving citation chips", async ({
  page,
}) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/search");

  await page.getByLabel("Question").fill("How do grace and faith relate?");
  await page.getByRole("button", { name: "Search" }).click();

  // The Summary card appears once the (stubbed) round-trip resolves.
  await expect(page.getByRole("heading", { name: "Summary" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible();

  // Two distinct sources -> two chips labelled [1] and [2], each linking to its
  // source card anchor. getByRole("link") scopes to the rendered <a> chips.
  const chip1 = page.getByRole("link", { name: "[1]" });
  const chip2 = page.getByRole("link", { name: "[2]" });
  await expect(chip1).toHaveAttribute("href", "#citation-1");
  await expect(chip2).toHaveAttribute("href", "#citation-2");

  // The anchors the chips point at exist (the <li> source cards carry the ids).
  await expect(page.locator("#citation-1")).toBeVisible();
  await expect(page.locator("#citation-2")).toBeVisible();

  // The source cards render the citation titles from the API payload.
  await expect(page.locator("#citation-1")).toContainText("On Grace");
  await expect(page.locator("#citation-2")).toContainText("Of Faith");
});

test("an empty query surfaces a client-side validation error and never searches", async ({
  page,
}) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/search");

  // Submit with an empty input: SearchPanel's searchQueryProblem fires before
  // any fetch, so the alert appears and no Summary card is rendered.
  await page.getByRole("button", { name: "Search" }).click();
  // Scope to SearchPanel's validation copy: a bare getByRole("alert") also
  // matches Next.js's always-present #__next-route-announcer__ (strict-mode
  // violation), so assert on the validation message text directly.
  await expect(page.getByText("Enter a question to search for.")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Summary" })).toHaveCount(0);
});

/**
 * Scoped search (Phase 49): tick a book on /library, jump to /search via "Search
 * these", and confirm the chosen scope rides over (the "N selected" label) and is
 * folded into the /search-summary POST as `book_ids` — an INTERSECTION the api
 * clamps to the library. The selection bridges the two routes through
 * sessionStorage (no query string).
 */
test("scoping to a selected library book carries book_ids into the summary search", async ({
  page,
}) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/library");

  // Tick one of the two seeded library books; the selection bar reflects it.
  await page.getByLabel("Select On Grace").check();
  await expect(page.getByTestId("selection-summary")).toHaveText(/1 book selected/);

  // "Search these" is a same-tab nav to /search; the selection persists.
  await page.getByRole("link", { name: "Search these" }).click();
  await expect(page).toHaveURL(/\/search$/);
  await expect(page.getByTestId("search-scope")).toHaveText("Searching 1 selected book.");

  // The summary POST must carry the selected book_ids (scope -> api intersection).
  const summaryRequest = page.waitForRequest(
    (req) => req.url().endsWith("/api/search-summary") && req.method() === "POST",
  );
  await page.getByLabel("Question").fill("How do grace and faith relate?");
  await page.getByRole("button", { name: "Search" }).click();
  const body = JSON.parse((await summaryRequest).postData() ?? "{}");
  expect(body.book_ids).toEqual(["11111111-1111-1111-1111-111111111111"]);

  // The (stubbed) scoped summary still renders.
  await expect(page.getByRole("heading", { name: "Summary" })).toBeVisible();
});

/**
 * Scoped search by COLLECTION (Phase 55): on /search directly, open the
 * Collections scope picker, tick a collection, and confirm its id is folded into
 * the /search-summary POST as `collection_ids` (the API resolves it to member
 * books and intersects with the JWT library). The picker drives the SAME shared
 * selection the Phase 49 book flow uses, so its one member book resolves the
 * "N selected" label. The fake api seeds one collection (Grace Collection).
 */
test("scoping to a selected collection carries collection_ids into the summary search", async ({
  page,
}) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/search");

  // Open the disclosure and tick the seeded collection; its single member book
  // resolves the scope label.
  await page.getByTestId("collection-scope-picker").click();
  await page.getByRole("checkbox", { name: /Grace Collection/ }).check();
  await expect(page.getByTestId("search-scope")).toHaveText("Searching 1 selected book.");

  // The summary POST must carry the chosen collection_ids (scope -> api intersection).
  const summaryRequest = page.waitForRequest(
    (req) => req.url().endsWith("/api/search-summary") && req.method() === "POST",
  );
  await page.getByLabel("Question").fill("How do grace and faith relate?");
  await page.getByRole("button", { name: "Search" }).click();
  const body = JSON.parse((await summaryRequest).postData() ?? "{}");
  expect(body.collection_ids).toEqual(["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]);

  // The (stubbed) scoped summary still renders.
  await expect(page.getByRole("heading", { name: "Summary" })).toBeVisible();
});
