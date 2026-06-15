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
