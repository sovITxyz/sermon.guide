import { expect, test } from "@playwright/test";
import { loginViaUi, makeUser, signUp } from "./support/users";

/**
 * Login -> upload -> task status, plus the Phase-20 task-ownership contract
 * asserted through the same-origin /api/tasks/{id} proxy: a user's OWN task
 * resolves 200, while ANOTHER user's task id is an indistinguishable 404 (no
 * existence oracle). The proxy passes the upstream status verbatim.
 */
test("login then upload reaches a terminal status in the UI", async ({ page }) => {
  const user = await signUp(page, makeUser());
  await loginViaUi(page, user, "/upload");

  // The file input is visually hidden (sr-only) but present; setInputFiles
  // drives it directly, which fires the same onChange the UI uses.
  await page.locator("#file-input").setInputFiles({
    name: "sample.epub",
    mimeType: "application/epub+zip",
    buffer: Buffer.from("fake epub bytes for the e2e upload"),
  });

  // The optimistic row shows the filename immediately, then polls to terminal.
  await expect(page.getByText("sample.epub")).toBeVisible();
  // Fake api / stub returns SUCCESS -> "Added to library" (taskLabel(done)).
  await expect(page.getByText("Added to library")).toBeVisible({ timeout: 15_000 });
});

test("the tasks proxy honors Phase-20 ownership: own task 200, another user's 404", async ({
  browser,
}) => {
  // Two independent browser contexts == two independent sessions/cookies.
  const ownerCtx = await browser.newContext();
  const otherCtx = await browser.newContext();
  try {
    const ownerPage = await ownerCtx.newPage();
    const otherPage = await otherCtx.newPage();

    const owner = await signUp(ownerPage, makeUser());
    const other = await signUp(otherPage, makeUser());
    await loginViaUi(ownerPage, owner, "/upload");
    await loginViaUi(otherPage, other, "/upload");

    // Owner uploads a file and captures the task_id from the upload proxy (202).
    const uploadRes = await ownerPage.request.post("/api/upload", {
      multipart: {
        file: {
          name: "owned.epub",
          mimeType: "application/epub+zip",
          buffer: Buffer.from("owned book bytes"),
        },
      },
    });
    expect(uploadRes.status()).toBe(202);
    const { task_id } = (await uploadRes.json()) as { task_id: string };
    expect(task_id).toBeTruthy();

    // Owner polling their OWN task -> 200 with a TaskStatus body.
    const ownLookup = await ownerPage.request.get(`/api/tasks/${encodeURIComponent(task_id)}`);
    expect(ownLookup.status()).toBe(200);
    const body = (await ownLookup.json()) as { task_id: string; status: string };
    expect(body.task_id).toBe(task_id);

    // A DIFFERENT user polling that same task id -> uniform 404 (no oracle):
    // identical to an entirely unknown id.
    const crossLookup = await otherPage.request.get(`/api/tasks/${encodeURIComponent(task_id)}`);
    expect(crossLookup.status()).toBe(404);

    const unknownLookup = await otherPage.request.get(
      `/api/tasks/${encodeURIComponent("00000000-0000-0000-0000-000000000000")}`,
    );
    expect(unknownLookup.status()).toBe(404);
  } finally {
    await ownerCtx.close();
    await otherCtx.close();
  }
});
