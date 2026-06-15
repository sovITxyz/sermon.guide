import { describe, expect, it } from "vitest";
import { whitelistCreateDocument, whitelistPatchDocument } from "../lib/documents";

const DOC = { type: "doc", content: [{ type: "paragraph" }] };

describe("whitelistCreateDocument", () => {
  it("forwards exactly title and content", () => {
    const result = whitelistCreateDocument({ title: "Sermon", content: DOC });
    expect(result).toEqual({ ok: true, body: { title: "Sermon", content: DOC } });
  });

  it("drops unknown keys — smuggled server-owned fields never reach the upstream body", () => {
    const result = whitelistCreateDocument({
      title: "Sermon",
      content: DOC,
      user_id: "11111111-1111-1111-1111-111111111111",
      content_text: "forged",
      schema_version: 99,
      document_id: "22222222-2222-2222-2222-222222222222",
      deleted_at: "2026-01-01T00:00:00Z",
    });
    expect(result).toEqual({ ok: true, body: { title: "Sermon", content: DOC } });
  });

  it("rejects non-object bodies (malformed JSON parses to null upstream of this)", () => {
    for (const body of [null, undefined, [], "{}", 12, true]) {
      expect(whitelistCreateDocument(body).ok).toBe(false);
    }
  });

  it("rejects a missing or non-string title", () => {
    expect(whitelistCreateDocument({ content: DOC }).ok).toBe(false);
    expect(whitelistCreateDocument({ title: 12, content: DOC }).ok).toBe(false);
  });

  it("rejects content that is not a JSON object (array / null / scalar)", () => {
    expect(whitelistCreateDocument({ title: "Sermon" }).ok).toBe(false);
    expect(whitelistCreateDocument({ title: "Sermon", content: [] }).ok).toBe(false);
    expect(whitelistCreateDocument({ title: "Sermon", content: null }).ok).toBe(false);
    expect(whitelistCreateDocument({ title: "Sermon", content: "x" }).ok).toBe(false);
  });

  it("leaves title length to the API — an empty title passes structurally", () => {
    // min_length=1 / max_length=512 is the API's 422 contract; the proxy
    // must not duplicate it.
    expect(whitelistCreateDocument({ title: "", content: DOC }).ok).toBe(true);
  });
});

describe("whitelistPatchDocument", () => {
  const BASE = "2026-06-14T12:00:00Z";

  it("forwards base_updated_at with title and content when both are present", () => {
    const result = whitelistPatchDocument({ base_updated_at: BASE, title: "New", content: DOC });
    expect(result).toEqual({
      ok: true,
      body: { base_updated_at: BASE, title: "New", content: DOC },
    });
  });

  it("omits an absent optional field rather than sending null", () => {
    const titleOnly = whitelistPatchDocument({ base_updated_at: BASE, title: "New" });
    expect(titleOnly).toEqual({ ok: true, body: { base_updated_at: BASE, title: "New" } });
    if (titleOnly.ok) {
      expect("content" in titleOnly.body).toBe(false);
    }

    const contentOnly = whitelistPatchDocument({ base_updated_at: BASE, content: DOC });
    expect(contentOnly).toEqual({ ok: true, body: { base_updated_at: BASE, content: DOC } });
    if (contentOnly.ok) {
      expect("title" in contentOnly.body).toBe(false);
    }
  });

  it("drops unknown keys — smuggled server-owned fields never reach the upstream body", () => {
    const result = whitelistPatchDocument({
      base_updated_at: BASE,
      content: DOC,
      user_id: "11111111-1111-1111-1111-111111111111",
      content_text: "forged",
      schema_version: 99,
      document_id: "22222222-2222-2222-2222-222222222222",
      deleted_at: "2026-01-01T00:00:00Z",
    });
    expect(result).toEqual({ ok: true, body: { base_updated_at: BASE, content: DOC } });
  });

  it("requires base_updated_at as a string", () => {
    expect(whitelistPatchDocument({ title: "New" }).ok).toBe(false);
    expect(whitelistPatchDocument({ base_updated_at: 123, title: "New" }).ok).toBe(false);
  });

  it("rejects wrong primitive types for the optional fields", () => {
    expect(whitelistPatchDocument({ base_updated_at: BASE, title: 12 }).ok).toBe(false);
    expect(whitelistPatchDocument({ base_updated_at: BASE, content: [] }).ok).toBe(false);
    expect(whitelistPatchDocument({ base_updated_at: BASE, content: "x" }).ok).toBe(false);
  });

  it("rejects non-object bodies", () => {
    for (const body of [null, undefined, [], "{}", 12, true]) {
      expect(whitelistPatchDocument(body).ok).toBe(false);
    }
  });

  it("leaves the at-least-one-of and length rules to the API — a base-only patch passes structurally", () => {
    // The API owns 'PATCH must set at least one of title or content.' (422)
    // and title min/max length; the proxy only guarantees the structural shape
    // + the required concurrency token.
    expect(whitelistPatchDocument({ base_updated_at: BASE }).ok).toBe(true);
    expect(whitelistPatchDocument({ base_updated_at: BASE, title: "" }).ok).toBe(true);
  });
});
