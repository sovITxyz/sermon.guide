import { describe, expect, it } from "vitest";
import { whitelistSearch } from "../lib/search";

describe("whitelistSearch", () => {
  it("forwards exactly query", () => {
    const result = whitelistSearch({ query: "grace and law" });
    expect(result).toEqual({ ok: true, body: { query: "grace and law" } });
  });

  it("drops limit and rerank — a client cannot widen fan-out or skip the pipeline", () => {
    const result = whitelistSearch({ query: "grace", limit: 100, rerank: false });
    expect(result).toEqual({ ok: true, body: { query: "grace" } });
  });

  it("drops smuggled tenant fields — they never reach the API's extra=forbid gate", () => {
    const result = whitelistSearch({
      query: "grace",
      user_id: "11111111-1111-1111-1111-111111111111",
      book_ids: ["22222222-2222-2222-2222-222222222222"],
    });
    expect(result).toEqual({ ok: true, body: { query: "grace" } });
  });

  it("rejects non-object bodies (malformed JSON parses to null upstream of this)", () => {
    for (const body of [null, undefined, [], "{}", 12, true]) {
      expect(whitelistSearch(body).ok).toBe(false);
    }
  });

  it("rejects a missing or non-string query", () => {
    expect(whitelistSearch({}).ok).toBe(false);
    expect(whitelistSearch({ query: 12 }).ok).toBe(false);
    expect(whitelistSearch({ query: null }).ok).toBe(false);
    expect(whitelistSearch({ limit: 10 }).ok).toBe(false);
  });

  it("leaves query length to the API — an empty query passes structurally", () => {
    // min_length=1 / max_length=1024 is the API's 422 contract; the proxy must
    // not duplicate it.
    expect(whitelistSearch({ query: "" }).ok).toBe(true);
    expect(whitelistSearch({ query: "x".repeat(2000) }).ok).toBe(true);
  });
});
