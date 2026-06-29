import { expect, test } from "@playwright/test";
import { loginViaUi, makeUser, signUp } from "./support/users";

/**
 * Schedule-from-sermon (Phase 47). The reverse of the calendar-first link flow:
 * from an OPEN sermon, the "📅 Schedule" toolbar button creates a calendar event
 * already linked to that sermon in ONE POST (the create proxy now forwards
 * `document_id`; the API ownership-checks it). This spec has NO backend schema
 * dependency — it rides the existing fake-api, which now accepts `document_id`
 * on POST /calendar/events.
 *
 * Flow asserted end-to-end:
 *   1. login -> create a sermon -> give it a distinctive title;
 *   2. open the Schedule popover, pick a date, submit;
 *   3. the inline confirmation appears with that date;
 *   4. "View on calendar" deep-links to the scheduled month;
 *   5. the event is LINKED — its chip click NAVIGATES back to the manuscript
 *      (not the edit popover), proving the one-POST create carried document_id.
 */

const SERMON_TITLE = "Resurrection Sunday";
const EVENT_DATE = "2031-05-04";

test("schedule a sermon from the editor: one-POST linked event appears on the calendar", async ({
  page,
}) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/sermons");

  // New sermon -> editor at /sermons/[id]; remember the URL to confirm the
  // linked chip routes back to THIS manuscript.
  await page.getByRole("button", { name: "New sermon" }).click();
  await page.waitForURL(/\/sermons\/[^/]+$/);
  const sermonUrl = page.url();

  // Give the sermon a distinctive title — the event title prefills from it —
  // and wait for the title autosave to settle so it survives the navigation
  // back here at the end (a client-side Link nav doesn't fire the pagehide
  // keepalive flush, so an in-flight debounce would otherwise be lost).
  await page.getByLabel("Sermon title").fill(SERMON_TITLE);
  const saveStatus = page.locator("[data-save-status]");
  await expect(saveStatus).not.toHaveAttribute("data-save-status", "saved");
  await expect(saveStatus).toHaveAttribute("data-save-status", "saved", { timeout: 15_000 });

  // Open the Schedule popover, set the date, submit.
  await page.getByRole("button", { name: "Schedule on calendar" }).click();
  const dialog = page.getByRole("dialog", { name: "Schedule on calendar" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel("Event title")).toHaveValue(SERMON_TITLE);
  await dialog.getByLabel("Date").fill(EVENT_DATE);
  await dialog.getByRole("button", { name: "Schedule" }).click();

  // The inline confirmation shows the scheduled date and a deep link.
  const confirmation = page.getByTestId("schedule-confirmation");
  await expect(confirmation).toBeVisible();
  await expect(confirmation).toContainText(EVENT_DATE);

  // Follow the deep link to the scheduled month.
  await confirmation.getByRole("link", { name: "View on calendar" }).click();
  await page.waitForURL(/\/calendar\?view=month&date=2031-05-04/);

  // The event is LINKED: clicking its chip navigates to the manuscript rather
  // than opening the edit popover — only possible if the create carried
  // document_id through the proxy + api.
  await page.getByRole("button", { name: `Edit ${SERMON_TITLE}` }).click();
  await page.waitForURL(/\/sermons\/.+/);
  expect(page.url()).toBe(sermonUrl);
  await expect(page.getByLabel("Sermon title")).toHaveValue(SERMON_TITLE);
  await expect(page.getByRole("dialog")).toBeHidden();
});

test("a non-blank series entered in the Schedule popover is saved on the event", async ({
  page,
}) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/sermons");

  await page.getByRole("button", { name: "New sermon" }).click();
  await page.waitForURL(/\/sermons\/[^/]+$/);
  await page.getByLabel("Sermon title").fill("Lenten Reflection");

  await page.getByRole("button", { name: "Schedule on calendar" }).click();
  const dialog = page.getByRole("dialog", { name: "Schedule on calendar" });
  await dialog.getByLabel("Date").fill("2031-03-09");
  await dialog.getByLabel(/series/i).fill("Lent");
  await dialog.getByRole("button", { name: "Schedule" }).click();
  await expect(page.getByTestId("schedule-confirmation")).toBeVisible();

  // Verify the event persisted with its series + link via the same range the
  // calendar fetches.
  const list = await page.request.get("/api/sermon-events?start=2031-03-01&end=2031-04-01");
  const { events } = (await list.json()) as {
    events: { title: string; series: string | null; document_id: string | null }[];
  };
  const target = events.find((e) => e.title === "Lenten Reflection");
  expect(target, "the scheduled event should be in range").toBeTruthy();
  expect(target?.series).toBe("Lent");
  expect(target?.document_id, "the event should be linked to the sermon").toBeTruthy();
});
