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

/**
 * Phase 40 — the week view + create/edit/delete round-trips.
 *
 * These exercise the same-origin mutation proxies (POST /api/sermon-events and
 * PATCH/DELETE /api/sermon-events/[eventId]) against the in-memory fake api,
 * which keeps a per-user store and materializes weekly repeats server-side. The
 * seeded 2028 rows are shared/visible to everyone, so each spec works in a fresh
 * future year (2029) and signs up a fresh user to assert only its OWN rows. The
 * UI refetches the current range after every successful mutation, so the new
 * state must appear without a manual reload.
 *
 * 2029-03-14 is a Wednesday → its week (Sunday-aligned) is 2029-03-11 .. 2029-03-17.
 */

const Y2 = "2029";
const CREATE_DATE = "2029-03-14";
const WEEK_ANCHOR = "2029-03-14"; // any day in the Sun 03-11 .. Sat 03-17 week

test("?view=week deep-links to a 7-column week grid (Sunday-aligned)", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  await page.goto(`/calendar?view=week&date=${WEEK_ANCHOR}`);

  // The week toggle is active and the week grid renders.
  await expect(page.getByRole("link", { name: "Week", exact: true })).toHaveAttribute(
    "aria-current",
    "page",
  );
  const weekGrid = page.getByTestId("calendar-week");
  await expect(weekGrid).toBeVisible();

  // Seven day columns, each with an "Add an event on YYYY-MM-DD" affordance for
  // its day — Sunday 03-11 first through Saturday 03-17 last.
  for (const day of [
    "2029-03-11",
    "2029-03-12",
    "2029-03-13",
    "2029-03-14",
    "2029-03-15",
    "2029-03-16",
    "2029-03-17",
  ]) {
    await expect(page.getByRole("button", { name: `Add an event on ${day}` })).toBeVisible();
  }
});

test("create on an empty day appears in the week, month, and year views", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  await page.goto(`/calendar?view=week&date=${WEEK_ANCHOR}`);
  await expect(page.getByTestId("calendar-week")).toBeVisible();

  // Open the quick-create popover for 2029-03-14 and fill it.
  await page.getByRole("button", { name: `Add an event on ${CREATE_DATE}` }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Title").fill("Midweek Vespers");
  await dialog.getByLabel("Series", { exact: false }).fill("Lent");
  await dialog.getByRole("button", { name: "Create" }).click();

  // After the POST the island refetches the week range — the new card shows up
  // in the week view without a reload.
  await expect(dialog).toBeHidden();
  await expect(page.getByTestId("calendar-week").getByText("Midweek Vespers")).toBeVisible();

  // The same row appears in the month view (March 2029)...
  await page.goto(`/calendar?view=month&date=${Y2}-03-01`);
  await expect(page.getByTestId("calendar-month").getByText("Midweek Vespers")).toBeVisible();

  // ...and in the year view (its March MiniMonth marks the day as having events).
  await page.goto(`/calendar?view=year&date=${Y2}-01-01`);
  const march = page.getByRole("region", { name: `March ${Y2}` });
  await expect(march.getByRole("group", { name: /2029-03-14, 1 event/ })).toBeVisible();
});

test("a weekly-repeat create materializes the whole capped run", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  // Create starting 2029-03-04 (a Sunday) repeating weekly until 2029-03-18 →
  // three rows: 03-04, 03-11, 03-18 (anchor + every +7 days through the until).
  await page.goto(`/calendar?view=month&date=${Y2}-03-01`);
  await expect(page.getByTestId("calendar-month")).toBeVisible();

  await page.getByRole("button", { name: "Add an event on 2029-03-04" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Title").fill("Lenten Series");
  await dialog.getByLabel("Repeat weekly", { exact: true }).check();
  await dialog.getByLabel("Repeat weekly until").fill("2029-03-18");
  await dialog.getByRole("button", { name: "Create" }).click();

  await expect(dialog).toBeHidden();
  // All three materialized rows land in the same month grid.
  await expect(page.getByTestId("calendar-month").getByText("Lenten Series")).toHaveCount(3);
});

test("a weekly-repeat over the cap surfaces the API's 422", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  await page.goto(`/calendar?view=month&date=${Y2}-01-01`);
  await expect(page.getByTestId("calendar-month")).toBeVisible();

  // 2029-01-07 .. 2031-01-12 is far more than 53 weekly occurrences → the API's
  // materializer cap 422 must surface inline (the proxy passes it through; the
  // UI never pre-validates the cap).
  await page.getByRole("button", { name: "Add an event on 2029-01-07" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Title").fill("Too Many");
  await dialog.getByLabel("Repeat weekly", { exact: true }).check();
  await dialog.getByLabel("Repeat weekly until").fill("2031-01-12");
  await dialog.getByRole("button", { name: "Create" }).click();

  // The dialog stays open and shows the cap error; nothing was created.
  await expect(dialog.getByRole("alert")).toContainText(/limit/i);
  await expect(dialog).toBeVisible();
});

test("edit a title then delete: round-trip reflected across views", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  // Seed one of our own rows via the week view.
  await page.goto(`/calendar?view=week&date=${WEEK_ANCHOR}`);
  await page.getByRole("button", { name: `Add an event on ${CREATE_DATE}` }).click();
  let dialog = page.getByRole("dialog");
  await dialog.getByLabel("Title").fill("Draft Sermon");
  await dialog.getByRole("button", { name: "Create" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByTestId("calendar-week").getByText("Draft Sermon")).toBeVisible();

  // Edit the title via the card's edit popover.
  await page.getByRole("button", { name: "Edit Draft Sermon" }).click();
  dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Title").fill("Final Sermon");
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(dialog).toBeHidden();

  // The rename is reflected in the week view (refetch) and the month view.
  await expect(page.getByTestId("calendar-week").getByText("Final Sermon")).toBeVisible();
  await expect(page.getByTestId("calendar-week").getByText("Draft Sermon")).toHaveCount(0);
  await page.goto(`/calendar?view=month&date=${Y2}-03-01`);
  await expect(page.getByTestId("calendar-month").getByText("Final Sermon")).toBeVisible();

  // Delete it from the month view's chip.
  await page.getByRole("button", { name: "Edit Final Sermon" }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "Delete" }).click();
  await expect(dialog).toBeHidden();
  // Gone from the month view and from the week view after the refetch.
  await expect(page.getByTestId("calendar-month").getByText("Final Sermon")).toHaveCount(0);
  await page.goto(`/calendar?view=week&date=${WEEK_ANCHOR}`);
  await expect(page.getByTestId("calendar-week").getByText("Final Sermon")).toHaveCount(0);
});

test("the same series renders the same color across week and month views", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  // Two events sharing one series on two days of the same week.
  await page.goto(`/calendar?view=week&date=${WEEK_ANCHOR}`);
  for (const [day, title] of [
    ["2029-03-12", "Service A"],
    ["2029-03-15", "Service B"],
  ] as const) {
    await page.getByRole("button", { name: `Add an event on ${day}` }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Title").fill(title);
    await dialog.getByLabel("Series", { exact: false }).fill("Eastertide");
    await dialog.getByRole("button", { name: "Create" }).click();
    await expect(dialog).toBeHidden();
  }

  // In the week view both cards share the deterministic series background class.
  const cardA = page.getByRole("button", { name: "Edit Service A" });
  const cardB = page.getByRole("button", { name: "Edit Service B" });
  const classA = (await cardA.getAttribute("class")) ?? "";
  const classB = (await cardB.getAttribute("class")) ?? "";
  const bgA = (classA.match(/bg-[a-z]+-100/) ?? [])[0];
  const bgB = (classB.match(/bg-[a-z]+-100/) ?? [])[0];
  expect(bgA).toBeTruthy();
  expect(bgA).toBe(bgB);

  // The month view uses the SAME mapper, so the chip background matches too.
  await page.goto(`/calendar?view=month&date=${Y2}-03-01`);
  const chip = page.getByRole("button", { name: "Edit Service A" });
  const chipClass = (await chip.getAttribute("class")) ?? "";
  const bgChip = (chipClass.match(/bg-[a-z]+-100/) ?? [])[0];
  expect(bgChip).toBe(bgA);
});

/**
 * Phase 41 — calendar ↔ manuscript linking (pure UX over the Phase 38 FK +
 * ownership check; no new endpoints). These drive the same-origin proxies:
 *   * link/unlink via PATCH /api/sermon-events/[id] with a three-state
 *     `document_id` (a string re-links, an explicit null unlinks);
 *   * create-doc-from-date = POST /api/sermon-events + POST /api/documents +
 *     PATCH document_id, then navigate into /sermons/[newId];
 *   * a LINKED chip/card click navigates straight to its manuscript;
 *   * the Phase 38 ownership 404 (a cross-tenant/nonexistent document_id)
 *     surfaces VISIBLY in the popover — never swallowed.
 *
 * Fresh year 2030 + fresh user per spec so only the user's OWN rows assert
 * (the 2028 seeds are shared and unlinked; the Phase 40 specs work in 2029).
 *
 * 2030-04-10 is a Wednesday → its week (Sunday-aligned) is 2030-04-07 .. 04-13.
 */

const Y3 = "2030";
const LINK_DATE = "2030-04-10";
const LINK_WEEK = "2030-04-10";

test("create-from-date: makes a draft, links the event, and opens the editor", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  // From an empty day, "Write a draft" creates the event, a draft sermon
  // titled after it, links them, and routes into the editor.
  await page.goto(`/calendar?view=week&date=${LINK_WEEK}`);
  await page.getByRole("button", { name: `Add an event on ${LINK_DATE}` }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Title").fill("Palm Sunday");
  await dialog.getByRole("button", { name: "Write a draft" }).click();

  // Lands in the editor for the brand-new doc, titled after the event.
  await page.waitForURL(/\/sermons\/.+/);
  await expect(page.getByLabel("Sermon title")).toHaveValue("Palm Sunday");

  // Back on the calendar the event is now LINKED: clicking its chip navigates
  // to the manuscript (it does NOT open the edit popover).
  await page.goto(`/calendar?view=week&date=${LINK_WEEK}`);
  await page.getByRole("button", { name: "Edit Palm Sunday" }).click();
  await page.waitForURL(/\/sermons\/.+/);
  await expect(page.getByLabel("Sermon title")).toHaveValue("Palm Sunday");
  // No edit dialog opened — the linked click navigated instead.
  await expect(page.getByRole("dialog")).toBeHidden();
});

test("link an existing sermon via the picker, then unlink it", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  // Seed a manuscript to link to: create-from-date on one day makes + links its
  // own event, leaving an owned doc the picker will offer for OTHER events.
  await page.goto(`/calendar?view=month&date=${Y3}-04-01`);
  await page.getByRole("button", { name: "Add an event on 2030-04-03" }).click();
  let dialog = page.getByRole("dialog");
  await dialog.getByLabel("Title").fill("Existing Manuscript");
  await dialog.getByRole("button", { name: "Write a draft" }).click();
  await page.waitForURL(/\/sermons\/.+/);

  // Create a SECOND, unlinked event we'll link to that manuscript via the picker.
  await page.goto(`/calendar?view=month&date=${Y3}-04-01`);
  await page.getByRole("button", { name: "Add an event on 2030-04-17" }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByLabel("Title").fill("Needs A Sermon");
  await dialog.getByRole("button", { name: "Create", exact: true }).click();
  await expect(dialog).toBeHidden();

  // Open the unlinked event's edit popover and LINK it via the picker.
  await page.getByRole("button", { name: "Edit Needs A Sermon" }).click();
  dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog
    .getByLabel("Linked sermon", { exact: false })
    .selectOption({ label: "Existing Manuscript" });
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(dialog).toBeHidden();

  // Now LINKED: the chip click navigates to the linked manuscript instead of
  // opening the popover.
  await page.getByRole("button", { name: "Edit Needs A Sermon" }).click();
  await page.waitForURL(/\/sermons\/.+/);
  await expect(page.getByLabel("Sermon title")).toHaveValue("Existing Manuscript");

  // UNLINK: a linked chip navigates, so the unlink PATCH (document_id: null) is
  // driven through the proxy directly — the three-state explicit null must reach
  // the API. Find the event id from the range the calendar fetches.
  const list = await page.request.get("/api/sermon-events?start=2030-04-01&end=2030-05-01");
  const { events } = (await list.json()) as { events: { event_id: string; title: string }[] };
  const target = events.find((e) => e.title === "Needs A Sermon");
  expect(target, "the linked event should be in range").toBeTruthy();
  const unlink = await page.request.patch(
    `/api/sermon-events/${encodeURIComponent(target?.event_id ?? "")}`,
    { data: { document_id: null } },
  );
  expect(unlink.ok(), "explicit null must pass the whitelist and detach").toBeTruthy();
  const unlinked = (await unlink.json()) as { document_id: string | null };
  expect(unlinked.document_id).toBeNull();

  // After the unlink the chip reverts to opening the edit popover (no navigate).
  await page.goto(`/calendar?view=month&date=${Y3}-04-01`);
  await page.getByRole("button", { name: "Edit Needs A Sermon" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
});

test("deleting the linked doc leaves the event alive and unlinked (ON DELETE SET NULL)", async ({
  page,
}) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  // Create-from-date makes the event + draft + link, then opens the editor.
  await page.goto(`/calendar?view=month&date=${Y3}-05-01`);
  await page.getByRole("button", { name: "Add an event on 2030-05-08" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Title").fill("Ascension Draft");
  await dialog.getByRole("button", { name: "Write a draft" }).click();
  await page.waitForURL(/\/sermons\/.+/);

  // Delete the manuscript from the /sermons list (soft delete + FK SET NULL).
  // The native confirm() fires synchronously on click, so the dialog handler
  // MUST be registered before the click.
  await page.goto("/sermons");
  page.once("dialog", (d) => void d.accept());
  await page.getByRole("button", { name: "Delete Ascension Draft" }).click();
  // The list refreshes (the row leaves; an undo toast appears).
  await expect(page.getByText("Deleted “Ascension Draft”.")).toBeVisible();

  // Back on the calendar the event SURVIVES and is now UNLINKED: clicking its
  // chip opens the edit popover (does NOT navigate, because document_id is null).
  await page.goto(`/calendar?view=month&date=${Y3}-05-01`);
  await expect(page.getByTestId("calendar-month").getByText("Ascension Draft")).toBeVisible();
  await page.getByRole("button", { name: "Edit Ascension Draft" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  // The picker shows "No linked sermon" selected (the deleted doc dropped out).
  await expect(page.getByRole("dialog").getByLabel("Linked sermon", { exact: false })).toHaveValue(
    "",
  );
});

test("a cross-tenant/nonexistent document_id surfaces the visible 404 (Phase 38)", async ({
  page,
}) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  // Create an unlinked event we'll try to (mis)link.
  await page.goto(`/calendar?view=month&date=${Y3}-06-01`);
  await page.getByRole("button", { name: "Add an event on 2030-06-12" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Title").fill("Mislink Target");
  await dialog.getByRole("button", { name: "Create", exact: true }).click();
  await expect(dialog).toBeHidden();

  // Drive the PATCH proxy DIRECTLY with a document_id the caller does NOT own
  // (a fixed UUID never created here) — the API ownership-checks it and returns
  // the no-oracle 404, which the proxy passes through {detail}. The picker only
  // ever offers OWNED docs, so this is the regression guard for the proxy path.
  // Find the event's id by reading the range the calendar fetched.
  const list = await page.request.get("/api/sermon-events?start=2030-06-01&end=2030-07-01");
  expect(list.ok()).toBeTruthy();
  const { events } = (await list.json()) as { events: { event_id: string; title: string }[] };
  const target = events.find((e) => e.title === "Mislink Target");
  expect(target, "the created event should be in range").toBeTruthy();
  const eventId = target?.event_id ?? "";

  const patch = await page.request.patch(`/api/sermon-events/${encodeURIComponent(eventId)}`, {
    data: { document_id: "99999999-9999-9999-9999-999999999999" },
  });
  // The Phase 38 no-oracle 404 passes through byte-for-byte.
  expect(patch.status()).toBe(404);
  const body = (await patch.json()) as { detail?: string };
  expect(typeof body.detail).toBe("string");

  // And the event is STILL unlinked (the failed link never landed): its chip
  // opens the edit popover rather than navigating.
  await page.goto(`/calendar?view=month&date=${Y3}-06-01`);
  await page.getByRole("button", { name: "Edit Mislink Target" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
});

/**
 * Phase 42 — calendar drag-to-reschedule (native HTML5 DnD, web-side only).
 *
 * The chips/cards are `draggable` and write their `event_id` to the
 * DataTransfer (`text/plain`) on dragstart; in-month day cells (year + month)
 * and week columns are drop targets that read it back and call CalendarView's
 * `onMove`, which OPTIMISTICALLY repositions the chip in local state and fires
 * EXACTLY ONE PATCH /api/sermon-events/{id} {event_date}. On success it trusts
 * the optimistic state (no refetch flash); on failure it rolls back to the EXACT
 * prior date and shows a NON-blocking error banner (the grid stays visible).
 *
 * Native DnD: Playwright's `locator.dragTo()` / `page.mouse` gestures do NOT
 * reliably synthesize the HTML5 drag protocol (dragstart/dragover/drop with a
 * working DataTransfer) in headless Chromium — a well-known limitation. So these
 * specs DISPATCH the DnD events manually via `page.evaluate`, threading ONE
 * shared `DataTransfer` through dragstart(chip) → dragover/drop(cell) so the
 * onDragStart payload reaches onDrop. Selectors use the component hooks:
 * draggables carry `[data-event-id]`, drop targets carry `[data-drop-date]`.
 *
 * Fresh future year 2031 + fresh user per spec (the 2028 seeds are shared; 2029
 * is Phase 40, 2030 is Phase 41). 2031-03-12 is a Wednesday → its Sunday-aligned
 * week is 2031-03-09 .. 2031-03-15.
 */

const Y4 = "2031";
const DRAG_SRC = "2031-03-12"; // Wednesday
const DRAG_DST = "2031-03-14"; // Friday, same Sun 03-09 .. Sat 03-15 week
const DRAG_WEEK = "2031-03-12";
// The fake api 500s any PATCH that would move an event onto this poison date
// (see e2e/support/fake-api.mjs FORCE_500_DATE) — the rollback trigger.
const POISON_SRC = "2031-12-21"; // Sunday
const POISON_DST = "2031-12-25"; // Thursday, same week + month as POISON_SRC

/**
 * Dispatch a native HTML5 drag from the chip carrying `eventId` onto the day
 * cell whose `data-drop-date` is `toDate`, threading one shared DataTransfer so
 * the dragstart payload reaches the drop. Returns true if both elements were
 * found and the events dispatched. This is the RELIABLE path in this harness —
 * `dragTo()` does not fire the native protocol in headless Chromium.
 */
async function dispatchDrag(
  page: import("@playwright/test").Page,
  eventId: string,
  toDate: string,
) {
  return page.evaluate(
    ({ eventId, toDate }) => {
      const chip = document.querySelector<HTMLElement>(`[data-event-id="${eventId}"]`);
      const cell = document.querySelector<HTMLElement>(`[data-drop-date="${toDate}"]`);
      if (!chip || !cell) {
        return { ok: false, hasChip: !!chip, hasCell: !!cell };
      }
      const dt = new DataTransfer();
      chip.dispatchEvent(
        new DragEvent("dragstart", { bubbles: true, cancelable: true, dataTransfer: dt }),
      );
      cell.dispatchEvent(
        new DragEvent("dragover", { bubbles: true, cancelable: true, dataTransfer: dt }),
      );
      cell.dispatchEvent(
        new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: dt }),
      );
      chip.dispatchEvent(
        new DragEvent("dragend", { bubbles: true, cancelable: true, dataTransfer: dt }),
      );
      return { ok: true, hasChip: true, hasCell: true };
    },
    { eventId, toDate },
  );
}

/** Read the user's own event id for `title` in a range (calendar's GET proxy). */
async function eventIdByTitle(
  page: import("@playwright/test").Page,
  title: string,
  start: string,
  end: string,
): Promise<string> {
  const res = await page.request.get(`/api/sermon-events?start=${start}&end=${end}`);
  expect(res.ok(), "range fetch should succeed").toBeTruthy();
  const { events } = (await res.json()) as { events: { event_id: string; title: string }[] };
  const found = events.find((e) => e.title === title);
  expect(found, `event "${title}" should be in range ${start}..${end}`).toBeTruthy();
  return found?.event_id ?? "";
}

test("a created event is visible in the year, month, and week views", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  // Create one of our own rows on 2031-03-12 via the week view.
  await page.goto(`/calendar?view=week&date=${DRAG_WEEK}`);
  await page.getByRole("button", { name: `Add an event on ${DRAG_SRC}` }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Title").fill("Reschedule Me");
  await dialog.getByRole("button", { name: "Create", exact: true }).click();
  await expect(dialog).toBeHidden();

  // Visible in the WEEK view (refetch, no manual reload).
  await expect(page.getByTestId("calendar-week").getByText("Reschedule Me")).toBeVisible();

  // ...the MONTH view (March 2031)...
  await page.goto(`/calendar?view=month&date=${Y4}-03-01`);
  await expect(page.getByTestId("calendar-month").getByText("Reschedule Me")).toBeVisible();

  // ...and the YEAR view (its March MiniMonth marks 2031-03-12 as having events).
  await page.goto(`/calendar?view=year&date=${Y4}-01-01`);
  const march = page.getByRole("region", { name: `March ${Y4}` });
  await expect(march.getByRole("group", { name: /2031-03-12, 1 event/ })).toBeVisible();
});

test("drag a chip to another day in the MONTH view: one PATCH, moves, and persists on reload", async ({
  page,
}) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  // Seed our own event on 2031-03-12.
  await page.goto(`/calendar?view=week&date=${DRAG_WEEK}`);
  await page.getByRole("button", { name: `Add an event on ${DRAG_SRC}` }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Title").fill("Month Drag");
  await dialog.getByRole("button", { name: "Create", exact: true }).click();
  await expect(dialog).toBeHidden();

  await page.goto(`/calendar?view=month&date=${Y4}-03-01`);
  const monthGrid = page.getByTestId("calendar-month");
  await expect(monthGrid.getByText("Month Drag")).toBeVisible();

  const eventId = await eventIdByTitle(page, "Month Drag", `${Y4}-03-01`, `${Y4}-04-01`);

  // The chip starts under 2031-03-12 and not under 2031-03-14.
  const srcCell = monthGrid.locator(`[data-drop-date="${DRAG_SRC}"]`);
  const dstCell = monthGrid.locator(`[data-drop-date="${DRAG_DST}"]`);
  await expect(srcCell.getByText("Month Drag")).toBeVisible();
  await expect(dstCell.getByText("Month Drag")).toHaveCount(0);

  // EXACTLY ONE PATCH fires for the drop. Count PATCHes to the single-event route.
  let patchCount = 0;
  page.on("request", (req) => {
    if (req.method() === "PATCH" && /\/api\/sermon-events\/[^/]+$/.test(req.url())) {
      patchCount += 1;
    }
  });

  const result = await dispatchDrag(page, eventId, DRAG_DST);
  expect(result.ok, "the chip and the destination cell must both be found").toBeTruthy();

  // The optimistic move repositions the chip under 2031-03-14 immediately.
  await expect(dstCell.getByText("Month Drag")).toBeVisible();
  await expect(srcCell.getByText("Month Drag")).toHaveCount(0);

  // Give any (erroneous) extra PATCH a chance to fire, then assert exactly one.
  await expect(page.getByTestId("calendar-month").getByText("Month Drag")).toBeVisible();
  expect(patchCount, "a single real move fires exactly one PATCH").toBe(1);

  // Reload from the (fake) api: the new date PERSISTED — the PATCH landed.
  await page.goto(`/calendar?view=month&date=${Y4}-03-01`);
  await expect(
    page
      .getByTestId("calendar-month")
      .locator(`[data-drop-date="${DRAG_DST}"]`)
      .getByText("Month Drag"),
  ).toBeVisible();
  await expect(
    page
      .getByTestId("calendar-month")
      .locator(`[data-drop-date="${DRAG_SRC}"]`)
      .getByText("Month Drag"),
  ).toHaveCount(0);
});

test("drag a card to another day in the WEEK view persists across a reload", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  await page.goto(`/calendar?view=week&date=${DRAG_WEEK}`);
  await page.getByRole("button", { name: `Add an event on ${DRAG_SRC}` }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Title").fill("Week Drag");
  await dialog.getByRole("button", { name: "Create", exact: true }).click();
  await expect(dialog).toBeHidden();

  const weekGrid = page.getByTestId("calendar-week");
  await expect(weekGrid.getByText("Week Drag")).toBeVisible();

  const eventId = await eventIdByTitle(page, "Week Drag", `${Y4}-03-01`, `${Y4}-04-01`);
  const srcCol = weekGrid.locator(`[data-drop-date="${DRAG_SRC}"]`);
  const dstCol = weekGrid.locator(`[data-drop-date="${DRAG_DST}"]`);
  await expect(srcCol.getByText("Week Drag")).toBeVisible();

  const result = await dispatchDrag(page, eventId, DRAG_DST);
  expect(result.ok).toBeTruthy();

  // Optimistic move into the Friday column.
  await expect(dstCol.getByText("Week Drag")).toBeVisible();
  await expect(srcCol.getByText("Week Drag")).toHaveCount(0);

  // Persisted: reload and confirm it sits under the new day.
  await page.goto(`/calendar?view=week&date=${DRAG_WEEK}`);
  await expect(
    page
      .getByTestId("calendar-week")
      .locator(`[data-drop-date="${DRAG_DST}"]`)
      .getByText("Week Drag"),
  ).toBeVisible();
  await expect(
    page
      .getByTestId("calendar-week")
      .locator(`[data-drop-date="${DRAG_SRC}"]`)
      .getByText("Week Drag"),
  ).toHaveCount(0);
});

test("drag in the YEAR view (popover row → day box) persists across a reload", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  await page.goto(`/calendar?view=week&date=${DRAG_WEEK}`);
  await page.getByRole("button", { name: `Add an event on ${DRAG_SRC}` }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Title").fill("Year Drag");
  await dialog.getByRole("button", { name: "Create", exact: true }).click();
  await expect(dialog).toBeHidden();

  await page.goto(`/calendar?view=year&date=${Y4}-01-01`);
  const march = page.getByRole("region", { name: `March ${Y4}` });
  // The 03-12 day box is an events popover; the 03-14 day box is currently a
  // bare (empty) in-month drop target.
  await expect(march.getByRole("group", { name: /2031-03-12, 1 event/ })).toBeVisible();

  const eventId = await eventIdByTitle(page, "Year Drag", `${Y4}-01-01`, "2032-01-01");

  // The draggable lives inside the day's <details> popover — open it so the
  // <li data-event-id> is in the DOM, then dispatch the drag onto the 03-14 box.
  await march
    .getByRole("group", { name: /2031-03-12, 1 event/ })
    .locator("summary")
    .click();
  const result = await dispatchDrag(page, eventId, DRAG_DST);
  expect(result.ok, "the popover row and the 03-14 day box must both be found").toBeTruthy();

  // After the move the 03-14 box now has events and 03-12 no longer does.
  await expect(march.getByRole("group", { name: /2031-03-14, 1 event/ })).toBeVisible();
  await expect(march.getByRole("group", { name: /2031-03-12/ })).toHaveCount(0);

  // Persisted on reload.
  await page.goto(`/calendar?view=year&date=${Y4}-01-01`);
  const marchReloaded = page.getByRole("region", { name: `March ${Y4}` });
  await expect(marchReloaded.getByRole("group", { name: /2031-03-14, 1 event/ })).toBeVisible();
});

test("a 500 on the move rolls the chip back to its original day and shows a visible error", async ({
  page,
}) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  // Seed an event on 2031-12-21; the poison destination 2031-12-25 is in the
  // same Sunday-aligned week and the same month.
  await page.goto(`/calendar?view=month&date=${Y4}-12-01`);
  await page.getByRole("button", { name: `Add an event on ${POISON_SRC}` }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Title").fill("Rollback Me");
  await dialog.getByRole("button", { name: "Create", exact: true }).click();
  await expect(dialog).toBeHidden();

  const monthGrid = page.getByTestId("calendar-month");
  await expect(monthGrid.getByText("Rollback Me")).toBeVisible();

  const eventId = await eventIdByTitle(page, "Rollback Me", `${Y4}-12-01`, "2032-01-01");
  const srcCell = monthGrid.locator(`[data-drop-date="${POISON_SRC}"]`);
  const dstCell = monthGrid.locator(`[data-drop-date="${POISON_DST}"]`);

  // Drag onto the poison date → the fake api 500s the PATCH.
  const result = await dispatchDrag(page, eventId, POISON_DST);
  expect(result.ok).toBeTruthy();

  // The chip SNAPS BACK to 2031-12-21 and a NON-blocking error banner appears.
  // Scope the alert to the red banner text (NOT a bare getByRole("alert") — the
  // App Router route announcer double-matches).
  await expect(page.getByText("Could not save the event.")).toBeVisible();
  await expect(srcCell.getByText("Rollback Me")).toBeVisible();
  await expect(dstCell.getByText("Rollback Me")).toHaveCount(0);

  // The grid is STILL visible (status stayed "ready"; this was not a blanking
  // error) — the calendar-month testid is still present alongside the banner.
  await expect(monthGrid).toBeVisible();

  // And the rollback was real: a reload still shows the event on its ORIGINAL
  // day (the 500 never persisted the move).
  await page.goto(`/calendar?view=month&date=${Y4}-12-01`);
  await expect(
    page
      .getByTestId("calendar-month")
      .locator(`[data-drop-date="${POISON_SRC}"]`)
      .getByText("Rollback Me"),
  ).toBeVisible();
});

test("keyboard-only reschedule via the edit popover's move-to-date fallback succeeds", async ({
  page,
}) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/calendar");

  // Seed an UNLINKED event (its chip opens the popover, which carries the
  // accessible reschedule control — HTML5 drag is mouse-only).
  await page.goto(`/calendar?view=month&date=${Y4}-03-01`);
  await page.getByRole("button", { name: `Add an event on ${DRAG_SRC}` }).click();
  let dialog = page.getByRole("dialog");
  await dialog.getByLabel("Title").fill("Keyboard Move");
  await dialog.getByRole("button", { name: "Create", exact: true }).click();
  await expect(dialog).toBeHidden();

  const monthGrid = page.getByTestId("calendar-month");
  await expect(
    monthGrid.locator(`[data-drop-date="${DRAG_SRC}"]`).getByText("Keyboard Move"),
  ).toBeVisible();

  // Open the edit popover from the chip via the KEYBOARD only: focus it and
  // press Enter (it is a native <button>, so it is focusable + Enter-activatable).
  const chip = page.getByRole("button", { name: "Edit Keyboard Move" });
  await chip.focus();
  await page.keyboard.press("Enter");
  dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();

  // Set the new date and submit via the dedicated "Move to date" button — no
  // mouse drag. The Date field is the accessible reschedule control; target the
  // date INPUT by role (the "Move to date" button's aria-label also contains
  // "date", so a substring getByLabel("Date") would be ambiguous).
  await dialog.getByRole("textbox", { name: "Date (reschedule)" }).fill(DRAG_DST);
  await dialog.getByRole("button", { name: "Move to date" }).click();

  // The popover closes, the island refetches, and the chip is now under the new
  // day in the month view.
  await expect(dialog).toBeHidden();
  await expect(
    page
      .getByTestId("calendar-month")
      .locator(`[data-drop-date="${DRAG_DST}"]`)
      .getByText("Keyboard Move"),
  ).toBeVisible();
  await expect(
    page
      .getByTestId("calendar-month")
      .locator(`[data-drop-date="${DRAG_SRC}"]`)
      .getByText("Keyboard Move"),
  ).toHaveCount(0);

  // Persisted on reload (the PATCH event_date landed).
  await page.goto(`/calendar?view=month&date=${Y4}-03-01`);
  await expect(
    page
      .getByTestId("calendar-month")
      .locator(`[data-drop-date="${DRAG_DST}"]`)
      .getByText("Keyboard Move"),
  ).toBeVisible();
});
