import { describe, expect, it } from "vitest";
import {
  MAX_QUERY_LENGTH,
  type SummarySegment,
  displaySection,
  formatElapsed,
  searchQueryProblem,
  segmentSummary,
  whitelistSummary,
} from "../lib/summary";

function joinSegments(segments: SummarySegment[]): string {
  return segments.map((s) => (s.kind === "text" ? s.text : s.marker)).join("");
}

describe("searchQueryProblem", () => {
  it("rejects empty and whitespace-only queries", () => {
    expect(searchQueryProblem("")).toBeTruthy();
    expect(searchQueryProblem("   \n\t ")).toBeTruthy();
  });

  it("accepts a normal question", () => {
    expect(searchQueryProblem("what does this say about grace?")).toBeNull();
  });

  it("enforces the API's 1024-char cap, inclusive", () => {
    expect(searchQueryProblem("q".repeat(MAX_QUERY_LENGTH))).toBeNull();
    expect(searchQueryProblem("q".repeat(MAX_QUERY_LENGTH + 1))).toBeTruthy();
  });
});

describe("segmentSummary", () => {
  it("resolves a known marker into a linked segment with surrounding text", () => {
    const segments = segmentSummary("Grace is central [Romans:7]. Indeed.", [
      { marker: "[Romans:7]" },
    ]);
    expect(segments).toEqual([
      { kind: "text", start: 0, text: "Grace is central " },
      { kind: "marker", start: 17, marker: "[Romans:7]", citationIndex: 0 },
      { kind: "text", start: 27, text: ". Indeed." },
    ]);
  });

  it("explodes a comma-merged bracket into one chip per resolved member (Phase 24)", () => {
    // The model sometimes merges adjacent citations into one bracket. The API
    // resolves the merged members (Phase 24), so each member that maps to a
    // returned citation must render as its own linked chip. The structural
    // `[`, `]`, and `, ` are dropped — merged brackets do not round-trip.
    const text = "Faith grows [Faith:70, Faith:51] over time.";
    const segments = segmentSummary(text, [{ marker: "[Faith:70]" }, { marker: "[Faith:51]" }]);
    expect(segments).toEqual([
      { kind: "text", start: 0, text: "Faith grows " },
      { kind: "marker", start: 13, marker: "[Faith:70]", citationIndex: 0 },
      { kind: "marker", start: 23, marker: "[Faith:51]", citationIndex: 1 },
      { kind: "text", start: 32, text: " over time." },
    ]);
  });

  it("drops only the invented member of a merged bracket, keeping real ones", () => {
    // A merged bracket can mix a real member with one the model invented; only
    // the resolvable members become chips — nothing is fabricated.
    const text = "Both views [A:1, Ghost:9, B:2] agree.";
    const segments = segmentSummary(text, [{ marker: "[A:1]" }, { marker: "[B:2]" }]);
    expect(segments).toEqual([
      { kind: "text", start: 0, text: "Both views " },
      { kind: "marker", start: 12, marker: "[A:1]", citationIndex: 0 },
      { kind: "marker", start: 26, marker: "[B:2]", citationIndex: 1 },
      { kind: "text", start: 30, text: " agree." },
    ]);
  });

  it("leaves a merged bracket as prose when no member resolves", () => {
    const text = "See [Ghost:1, Phantom:2] later.";
    expect(segmentSummary(text, [{ marker: "[A:1]" }])).toEqual([{ kind: "text", start: 0, text }]);
  });

  it("links single and merged brackets in document order", () => {
    const text = "First [A:1] then merged [B:2, C:3] last [D:4].";
    const segments = segmentSummary(text, [
      { marker: "[A:1]" },
      { marker: "[B:2]" },
      { marker: "[C:3]" },
      { marker: "[D:4]" },
    ]);
    expect(segments.filter((s) => s.kind === "marker")).toEqual([
      { kind: "marker", start: 6, marker: "[A:1]", citationIndex: 0 },
      { kind: "marker", start: 25, marker: "[B:2]", citationIndex: 1 },
      { kind: "marker", start: 30, marker: "[C:3]", citationIndex: 2 },
      { kind: "marker", start: 40, marker: "[D:4]", citationIndex: 3 },
    ]);
    // Marker `start`s are strictly increasing — unique, stable React keys.
    const starts = segments.filter((s) => s.kind === "marker").map((s) => s.start);
    expect(starts).toEqual([...starts].sort((a, b) => a - b));
    expect(new Set(starts).size).toBe(starts.length);
  });

  it("leaves invented markers as plain text", () => {
    const text = "See [Nonexistent:7] for details.";
    expect(segmentSummary(text, [{ marker: "[Faith:0]" }])).toEqual([
      { kind: "text", start: 0, text },
    ]);
  });

  it("links every occurrence of a repeated marker", () => {
    const segments = segmentSummary("A [X:1] B [X:1]", [{ marker: "[X:1]" }]);
    expect(segments.filter((s) => s.kind === "marker")).toHaveLength(2);
  });

  it("does not match a marker inside a longer one ([X:1] vs [X:12])", () => {
    const segments = segmentSummary("Only [X:12] cited.", [
      { marker: "[X:1]" },
      { marker: "[X:12]" },
    ]);
    expect(segments.filter((s) => s.kind === "marker")).toEqual([
      { kind: "marker", start: 5, marker: "[X:12]", citationIndex: 1 },
    ]);
  });

  it("handles markers at the start, end, and adjacent to each other", () => {
    const segments = segmentSummary("[A:1] then [A:1][B:2]", [
      { marker: "[A:1]" },
      { marker: "[B:2]" },
    ]);
    expect(joinSegments(segments)).toBe("[A:1] then [A:1][B:2]");
    expect(segments[0]).toEqual({ kind: "marker", start: 0, marker: "[A:1]", citationIndex: 0 });
    expect(segments.at(-1)).toEqual({
      kind: "marker",
      start: 16,
      marker: "[B:2]",
      citationIndex: 1,
    });
  });

  it("round-trips standalone markers: concatenated segments reproduce the input", () => {
    // With no exploded merged bracket, concatenating segments is lossless. The
    // unmatched merged bracket here stays prose because no member resolves.
    const text = "Start [A:1] middle [Faith:70, Faith:51] then [B:2] end.";
    const segments = segmentSummary(text, [{ marker: "[A:1]" }, { marker: "[B:2]" }]);
    expect(joinSegments(segments)).toBe(text);
  });

  it("returns a single text segment when there are no citations", () => {
    expect(segmentSummary("No sources here.", [])).toEqual([
      { kind: "text", start: 0, text: "No sources here." },
    ]);
  });

  it("returns no segments for an empty summary", () => {
    expect(segmentSummary("", [])).toEqual([]);
  });
});

describe("displaySection", () => {
  it("passes a clean section label through, trimmed", () => {
    expect(displaySection("  Book III, Chapter 11  ")).toBe("Book III, Chapter 11");
  });

  it("drops null, empty, and whitespace-only sections", () => {
    expect(displaySection(null)).toBeNull();
    expect(displaySection("")).toBeNull();
    expect(displaySection("   ")).toBeNull();
  });

  it("drops EPUB-extraction HTML debris (Phase 16 live finding)", () => {
    expect(displaySection('<a href="part0002.html#pt03ch_11" class="calibre4"><span')).toBeNull();
    expect(displaySection("Faith <b>and</b> Works")).toBeNull();
  });
});

describe("formatElapsed", () => {
  it("formats seconds as m:ss", () => {
    expect(formatElapsed(0)).toBe("0:00");
    expect(formatElapsed(7)).toBe("0:07");
    expect(formatElapsed(61)).toBe("1:01");
    expect(formatElapsed(134)).toBe("2:14");
  });

  it("clamps negatives and floors fractions", () => {
    expect(formatElapsed(-5)).toBe("0:00");
    expect(formatElapsed(9.9)).toBe("0:09");
  });
});

describe("whitelistSummary", () => {
  it("forwards exactly query when no scope is present (= whole library)", () => {
    expect(whitelistSummary({ query: "grace" })).toEqual({ ok: true, body: { query: "grace" } });
  });

  it("forwards the Phase 49 scope arrays when present", () => {
    expect(whitelistSummary({ query: "grace", book_ids: ["b1"], collection_ids: ["c1"] })).toEqual({
      ok: true,
      body: { query: "grace", book_ids: ["b1"], collection_ids: ["c1"] },
    });
  });

  it("drops a smuggled user_id / limit_chunks but keeps the scope", () => {
    const result = whitelistSummary({
      query: "grace",
      user_id: "u1",
      limit_chunks: 99,
      book_ids: ["b1"],
    });
    expect(result).toEqual({ ok: true, body: { query: "grace", book_ids: ["b1"] } });
  });

  it("treats a null scope field as absent and omits it", () => {
    expect(whitelistSummary({ query: "grace", book_ids: null })).toEqual({
      ok: true,
      body: { query: "grace" },
    });
  });

  it("rejects a non-object body, a non-string query, and a non-array scope", () => {
    expect(whitelistSummary(null).ok).toBe(false);
    expect(whitelistSummary([]).ok).toBe(false);
    expect(whitelistSummary({}).ok).toBe(false);
    expect(whitelistSummary({ query: 12 }).ok).toBe(false);
    expect(whitelistSummary({ query: "grace", collection_ids: "c1" }).ok).toBe(false);
    expect(whitelistSummary({ query: "grace", book_ids: [1] }).ok).toBe(false);
  });

  it("leaves query length to the API — an empty query passes structurally", () => {
    expect(whitelistSummary({ query: "" }).ok).toBe(true);
    expect(whitelistSummary({ query: "x".repeat(MAX_QUERY_LENGTH + 100) }).ok).toBe(true);
  });
});
