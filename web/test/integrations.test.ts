import { describe, expect, it } from "vitest";
import { ALLOWED_PROVIDERS, isAllowedProvider, settingsRedirectPath } from "../lib/integrations";

/**
 * Unit tests for the OAuth integrations pure helpers (Phase 44). These pin the
 * provider allow-set (the gate that keeps the {provider} path param from
 * reaching the API as arbitrary text) and the FIXED, non-attacker-controlled
 * settings redirect target (the open-redirect defense in the public callback).
 */

describe("isAllowedProvider", () => {
  it("accepts the sanctioned providers", () => {
    for (const provider of ALLOWED_PROVIDERS) {
      expect(isAllowedProvider(provider)).toBe(true);
    }
    expect(isAllowedProvider("google")).toBe(true);
  });

  it("rejects unknown / not-yet-configured providers", () => {
    expect(isAllowedProvider("microsoft")).toBe(false); // Phase 46, not yet
    expect(isAllowedProvider("evil")).toBe(false);
    expect(isAllowedProvider("")).toBe(false);
    expect(isAllowedProvider("GOOGLE")).toBe(false); // case-sensitive
    expect(isAllowedProvider("google/../etc")).toBe(false);
  });
});

describe("settingsRedirectPath", () => {
  it("builds the fixed success path with the connected provider", () => {
    expect(settingsRedirectPath({ connected: "google" })).toBe(
      "/settings/integrations?connected=google",
    );
  });

  it("builds the fixed error path with a generic code", () => {
    expect(settingsRedirectPath({ error: "denied" })).toBe("/settings/integrations?error=denied");
    expect(settingsRedirectPath({ error: "failed" })).toBe("/settings/integrations?error=failed");
  });

  it("always targets the same-origin settings path (no open redirect)", () => {
    // Whatever the inputs, the path is a constant prefix — there is no way to
    // steer the browser off the settings page.
    const success = settingsRedirectPath({ connected: "google" });
    const failure = settingsRedirectPath({ error: "failed" });
    expect(success.startsWith("/settings/integrations?")).toBe(true);
    expect(failure.startsWith("/settings/integrations?")).toBe(true);
    // No protocol/host can be injected — these are relative paths only.
    expect(success).not.toContain("//");
    expect(failure).not.toContain("//");
  });

  it("URL-encodes the error code so a crafted code cannot break out of the query", () => {
    // settingsRedirectPath only ever receives our own fixed codes, but pin that
    // URLSearchParams encoding is in force as a belt-and-suspenders guard.
    expect(settingsRedirectPath({ error: "a b&c=d" })).toBe(
      "/settings/integrations?error=a+b%26c%3Dd",
    );
  });
});
