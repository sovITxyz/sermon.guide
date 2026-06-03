import { describe, expect, it } from "vitest";
import {
  SESSION_COOKIE,
  SESSION_MAX_AGE_SECONDS,
  clearedSessionCookieOptions,
  sessionCookieOptions,
} from "../lib/session";

describe("sessionCookieOptions", () => {
  it("is HttpOnly, Lax, root-path so the JWT is never client-readable", () => {
    const opts = sessionCookieOptions(false);
    expect(opts.httpOnly).toBe(true);
    expect(opts.sameSite).toBe("lax");
    expect(opts.path).toBe("/");
    expect(opts.maxAge).toBe(SESSION_MAX_AGE_SECONDS);
  });

  it("sets Secure only in production", () => {
    expect(sessionCookieOptions(true).secure).toBe(true);
    expect(sessionCookieOptions(false).secure).toBe(false);
  });

  it("clears with maxAge 0 while keeping the same scoping attributes", () => {
    const cleared = clearedSessionCookieOptions(true);
    expect(cleared.maxAge).toBe(0);
    expect(cleared.httpOnly).toBe(true);
    expect(cleared.secure).toBe(true);
    expect(cleared.path).toBe("/");
  });

  it("exposes a stable cookie name", () => {
    expect(SESSION_COOKIE).toBe("sg_session");
  });
});
