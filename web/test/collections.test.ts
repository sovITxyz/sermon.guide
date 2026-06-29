import { describe, expect, it } from "vitest";
import {
  whitelistCollectionBooks,
  whitelistCreateCollection,
  whitelistPatchCollection,
} from "../lib/collections";

describe("whitelistCreateCollection", () => {
  it("forwards exactly name when only the required field is set", () => {
    const result = whitelistCreateCollection({ name: "Commentaries" });
    expect(result).toEqual({ ok: true, body: { name: "Commentaries" } });
  });

  it("forwards description when present (incl. null)", () => {
    expect(whitelistCreateCollection({ name: "Romans", description: "Pauline corpus" })).toEqual({
      ok: true,
      body: { name: "Romans", description: "Pauline corpus" },
    });
    expect(whitelistCreateCollection({ name: "Romans", description: null })).toEqual({
      ok: true,
      body: { name: "Romans", description: null },
    });
  });

  it("omits an absent description rather than sending null", () => {
    const result = whitelistCreateCollection({ name: "Romans" });
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect("description" in result.body).toBe(false);
    }
  });

  it("drops unknown keys (a smuggled user_id / collection_id / created_at)", () => {
    const result = whitelistCreateCollection({
      name: "Romans",
      user_id: "33333333-3333-3333-3333-333333333333",
      collection_id: "11111111-1111-1111-1111-111111111111",
      created_at: "2026-01-01T00:00:00Z",
    });
    expect(result).toEqual({ ok: true, body: { name: "Romans" } });
  });

  it("rejects a missing / non-string name", () => {
    expect(whitelistCreateCollection({}).ok).toBe(false);
    expect(whitelistCreateCollection({ name: 12 }).ok).toBe(false);
    expect(whitelistCreateCollection({ name: null }).ok).toBe(false);
  });

  it("rejects a description that is neither string nor null", () => {
    expect(whitelistCreateCollection({ name: "x", description: 12 }).ok).toBe(false);
    expect(whitelistCreateCollection({ name: "x", description: {} }).ok).toBe(false);
  });

  it("rejects non-object bodies (malformed JSON parses to null upstream of this)", () => {
    for (const body of [null, undefined, [], "{}", 12, true]) {
      expect(whitelistCreateCollection(body).ok).toBe(false);
    }
  });

  it("leaves length validation to the API — structurally-valid junk passes", () => {
    // Empty name and an over-long description are the API's 422 to own.
    expect(whitelistCreateCollection({ name: "" }).ok).toBe(true);
    expect(whitelistCreateCollection({ name: "x", description: "y".repeat(9999) }).ok).toBe(true);
  });
});

describe("whitelistPatchCollection", () => {
  it("forwards name and/or description when present", () => {
    expect(whitelistPatchCollection({ name: "Renamed" })).toEqual({
      ok: true,
      body: { name: "Renamed" },
    });
    expect(whitelistPatchCollection({ name: "Renamed", description: "new" })).toEqual({
      ok: true,
      body: { name: "Renamed", description: "new" },
    });
  });

  it("omits absent fields — present-only partial semantics", () => {
    const descOnly = whitelistPatchCollection({ description: "just the description" });
    expect(descOnly).toEqual({ ok: true, body: { description: "just the description" } });
    if (descOnly.ok) {
      expect("name" in descOnly.body).toBe(false);
    }
  });

  it("forwards description: null verbatim to CLEAR it (a meaningful value, not truthiness)", () => {
    // The single most likely defect: a truthiness guard would DROP this null and
    // silently break the clear. Key-presence must let an explicit null pass.
    const result = whitelistPatchCollection({ description: null });
    expect(result).toEqual({ ok: true, body: { description: null } });
    if (result.ok) {
      expect("description" in result.body).toBe(true);
      expect(result.body.description).toBeNull();
    }
  });

  it("rejects a non-string name (incl. null — the column is NOT NULL)", () => {
    expect(whitelistPatchCollection({ name: 12 }).ok).toBe(false);
    expect(whitelistPatchCollection({ name: null }).ok).toBe(false);
  });

  it("rejects a description that is neither string nor null", () => {
    expect(whitelistPatchCollection({ description: 12 }).ok).toBe(false);
    expect(whitelistPatchCollection({ description: [] }).ok).toBe(false);
  });

  it("drops unknown keys (collection_id / user_id / created_at / book_ids)", () => {
    const result = whitelistPatchCollection({
      name: "Renamed",
      collection_id: "11111111-1111-1111-1111-111111111111",
      user_id: "33333333-3333-3333-3333-333333333333",
      book_ids: ["x"],
    });
    expect(result).toEqual({ ok: true, body: { name: "Renamed" } });
  });

  it("rejects non-object bodies", () => {
    for (const body of [null, undefined, [], "{}", 12, true]) {
      expect(whitelistPatchCollection(body).ok).toBe(false);
    }
  });

  it("leaves the at-least-one-of and length rules to the API — an empty patch passes structurally", () => {
    expect(whitelistPatchCollection({}).ok).toBe(true);
    expect(whitelistPatchCollection({ name: "" }).ok).toBe(true);
  });
});

describe("whitelistCollectionBooks", () => {
  it("forwards book_ids as a fresh array of strings", () => {
    const result = whitelistCollectionBooks({
      book_ids: ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"],
    });
    expect(result).toEqual({
      ok: true,
      body: {
        book_ids: ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"],
      },
    });
  });

  it("drops unknown keys (a smuggled user_id / collection_id)", () => {
    const result = whitelistCollectionBooks({
      book_ids: ["11111111-1111-1111-1111-111111111111"],
      user_id: "33333333-3333-3333-3333-333333333333",
      collection_id: "44444444-4444-4444-4444-444444444444",
    });
    expect(result).toEqual({
      ok: true,
      body: { book_ids: ["11111111-1111-1111-1111-111111111111"] },
    });
  });

  it("rejects a missing book_ids or one that is not an array", () => {
    expect(whitelistCollectionBooks({}).ok).toBe(false);
    expect(whitelistCollectionBooks({ book_ids: "x" }).ok).toBe(false);
    expect(whitelistCollectionBooks({ book_ids: {} }).ok).toBe(false);
  });

  it("rejects an array with a non-string element", () => {
    expect(whitelistCollectionBooks({ book_ids: ["ok", 12] }).ok).toBe(false);
    expect(whitelistCollectionBooks({ book_ids: [null] }).ok).toBe(false);
  });

  it("rejects non-object bodies", () => {
    for (const body of [null, undefined, [], "{}", 12, true]) {
      expect(whitelistCollectionBooks(body).ok).toBe(false);
    }
  });

  it("leaves the 1..10000 cap to the API — an empty array passes structurally", () => {
    const result = whitelistCollectionBooks({ book_ids: [] });
    expect(result).toEqual({ ok: true, body: { book_ids: [] } });
  });
});
