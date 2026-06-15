import { randomUUID } from "node:crypto";
import type { Page } from "@playwright/test";
import { expect } from "@playwright/test";

/**
 * Throwaway test-user helpers. Credentials are generated per-run with a random
 * UUID so nothing real is ever committed and parallel specs never collide. They
 * are created against the same backend the web dev server proxies to (the fake
 * api in CI, the real api on the live path).
 */

export interface TestUser {
  email: string;
  password: string;
}

export function makeUser(): TestUser {
  return { email: `e2e-${randomUUID()}@example.com`, password: `pw-${randomUUID()}` };
}

/** Sign up via the same-origin proxy, then return the credentials. */
export async function signUp(page: Page, user: TestUser = makeUser()): Promise<TestUser> {
  const res = await page.request.post("/api/auth/signup", {
    data: { email: user.email, password: user.password },
  });
  expect(res.status(), "signup should be accepted").toBe(201);
  return user;
}

/**
 * Drive the real login UI: fill the form, submit, and wait for the post-login
 * navigation. The HttpOnly session cookie is planted by the login proxy — the
 * JWT never reaches the browser.
 */
export async function loginViaUi(page: Page, user: TestUser, next = "/search"): Promise<void> {
  await page.goto(`/login?next=${encodeURIComponent(next)}`);
  await page.getByLabel("Email").fill(user.email);
  await page.getByLabel("Password").fill(user.password);
  await page.getByRole("button", { name: "Log in" }).click();
  await page.waitForURL(`**${next}`);
}
