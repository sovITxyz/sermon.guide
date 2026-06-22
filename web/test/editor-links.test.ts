import { describe, expect, it } from "vitest";
import { isUnlinkMode, whitelistUnlink } from "../lib/editor-links";

/**
 * Pure-helper unit tests for the editor-link unlink whitelist (Phase 45). The
 * load-bearing property: the unlink proxy forwards EXACTLY `{mode}` with a
 * closed value set (`pull-final` | `keep-app`) and drops everything else — a
 * smuggled `provider_file_id`, `document_id`, or `user_id` never survives to the
 * upstream body, and an out-of-set mode is rejected here before the API.
 */

describe("isUnlinkMode", () => {
  it("accepts exactly the two settled modes", () => {
    expect(isUnlinkMode("pull-final")).toBe(true);
    expect(isUnlinkMode("keep-app")).toBe(true);
  });

  it("rejects anything else", () => {
    for (const value of [
      "",
      "PULL-FINAL",
      "keep",
      "delete",
      1,
      null,
      undefined,
      {},
      ["pull-final"],
    ]) {
      expect(isUnlinkMode(value)).toBe(false);
    }
  });
});

describe("whitelistUnlink", () => {
  it("forwards exactly { mode } for pull-final", () => {
    expect(whitelistUnlink({ mode: "pull-final" })).toEqual({
      ok: true,
      body: { mode: "pull-final" },
    });
  });

  it("forwards exactly { mode } for keep-app", () => {
    expect(whitelistUnlink({ mode: "keep-app" })).toEqual({
      ok: true,
      body: { mode: "keep-app" },
    });
  });

  it("drops smuggled keys — only mode reaches the upstream body", () => {
    const result = whitelistUnlink({
      mode: "keep-app",
      provider_file_id: "attacker-drive-file",
      document_id: "22222222-2222-2222-2222-222222222222",
      user_id: "11111111-1111-1111-1111-111111111111",
      web_url: "https://evil.example/doc",
      state: "linked",
    });
    expect(result).toEqual({ ok: true, body: { mode: "keep-app" } });
  });

  it("rejects an out-of-set or missing mode (no upstream forward)", () => {
    expect(whitelistUnlink({ mode: "delete" }).ok).toBe(false);
    expect(whitelistUnlink({ mode: "" }).ok).toBe(false);
    expect(whitelistUnlink({ mode: 1 }).ok).toBe(false);
    expect(whitelistUnlink({}).ok).toBe(false);
  });

  it("rejects non-object bodies (malformed JSON parses to null upstream of this)", () => {
    for (const body of [null, undefined, [], "{}", 12, true]) {
      expect(whitelistUnlink(body).ok).toBe(false);
    }
  });
});
