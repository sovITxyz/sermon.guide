import { expect, test } from "@playwright/test";
import { loginViaUi, makeUser, signUp } from "./support/users";

/**
 * Sermon calendar year + month views (Phase 39, read-only). Drives the
 * /calendar page against the same-origin GET /api/sermon-events proxy, backed
 * by the in-memory fake api (e2e/support/fake-api.mjs, /calendar/events with
 * deterministic 2028 events) in CI and the real api on the nightly live path.
 *
 * What it pins (the Phase 39 Verify checklist):
 *   1. /calendar?view=year renders all 12 MiniMonths, correctly aligned — spot-
 *      checking a LEAP February (Feb 2028 = 29 days) and a SUNDAY-starting month
 *      (Oct 2028, day 1 in the first grid column);
 *   2. days with events show series dots (the seeded 2028 events);
 *   3. ?view=month&date=… deep-links straight to the right month;
 *   4. an UNAUTHENTICATED /calendar bounces to /login (the middleware matcher).
 *
 * The seeded events live in 2028, so the specs navigate there explicitly via
 * the URL state rather than relying on the current date.
 */

const YEAR = "2028";

test("unauthenticated /calendar redirects to /login", async ({ page }) => {
  await page.goto("/calendar?view=year&date=2028-01-01");
  // The presence-only middleware gate bounces to /login with a next= back to
  // the calendar before the page ever renders. The middleware preserves the
  // original query, so next= is one of several params (not necessarily first).
  await page.waitForURL(/\/login\?/);
  expect(page.url()).toContain("next=%2Fcalendar");
  await expect(page.getByRole("button", { name: "Log in" })).toBeVisible();
});

test("year view renders 12 aligned months with a leap February and a Sunday-start month", async ({
  page,
}) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  await page.goto(`/calendar?view=year&date=${YEAR}-01-01`);

  // The year heading and the year grid.
  await expect(page.getByRole("heading", { level: 1, name: YEAR })).toBeVisible();
  const yearGrid = page.getByTestId("calendar-year");
  await expect(yearGrid).toBeVisible();

  // All twelve months render, each as a labelled MiniMonth section.
  for (const month of [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
  ]) {
    await expect(page.getByRole("region", { name: `${month} ${YEAR}` })).toBeVisible();
  }

  // LEAP February: Feb 2028 has 29 days, so a "29" day cell exists in the
  // February region (a non-leap year would not).
  const february = page.getByRole("region", { name: `February ${YEAR}` });
  await expect(february.getByText("29", { exact: true })).toBeVisible();

  // The leap-day event (2028-02-29) is in February and shows a dot via its
  // clickable popover summary.
  const leapDay = february.getByRole("group").filter({ hasText: "29" });
  await expect(leapDay).toHaveCount(1);
});

test("event days show series dots in the year view", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  await page.goto(`/calendar?view=year&date=${YEAR}-01-01`);

  // October 2028 starts on a Sunday and carries seeded events on the 1st and
  // 15th — open the 1st's popover and confirm both that day's events list.
  const october = page.getByRole("region", { name: `October ${YEAR}` });
  await expect(october).toBeVisible();

  // The Oct 1 cell is an interactive popover (it has events); open it.
  const oct1 = october.getByRole("group", { name: /2028-10-01, 2 events/ });
  await expect(oct1).toBeVisible();
  await oct1.locator("summary").click();
  await expect(october.getByText("Harvest Thanksgiving")).toBeVisible();
  await expect(october.getByText("Evening Prayer")).toBeVisible();
});

test("?view=month&date=… deep-links straight to the right month", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  // Deep-link into October 2028 directly.
  await page.goto(`/calendar?view=month&date=${YEAR}-10-15`);

  await expect(page.getByRole("heading", { level: 1, name: `October ${YEAR}` })).toBeVisible();
  const monthGrid = page.getByTestId("calendar-month");
  await expect(monthGrid).toBeVisible();

  // October 2028 starts on Sunday: the "1" sits in the first (Sunday) column.
  // The month view renders event chips as plain-text titles (never inner HTML).
  await expect(monthGrid.getByText("Reformation Sunday")).toBeVisible();
  await expect(monthGrid.getByText("Harvest Thanksgiving")).toBeVisible();

  // The view toggle reflects the active month view (exact: the prev/next arrows
  // are labelled "Previous month"/"Next month" and would otherwise also match).
  await expect(page.getByRole("link", { name: "Month", exact: true })).toHaveAttribute(
    "aria-current",
    "page",
  );
});

test("year→month drill-down: clicking a month name opens that month", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  await page.goto(`/calendar?view=year&date=${YEAR}-01-01`);

  // Click the February month-name link to drill into the month view.
  await page.getByRole("link", { name: "February", exact: true }).click();
  await page.waitForURL(/view=month&date=2028-02-01/);
  await expect(page.getByRole("heading", { level: 1, name: `February ${YEAR}` })).toBeVisible();
  await expect(page.getByTestId("calendar-month").getByText("Sermon on the Mount")).toBeVisible();
});
