import { describe, expect, it } from "vitest";
import {
  MAX_QUERY_LENGTH,
  type SummarySegment,
  displaySection,
  formatElapsed,
  searchQueryProblem,
  segmentSummary,
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

  it("leaves comma-merged brackets as plain text (Phase 14b live finding)", () => {
    // The model sometimes merges adjacent citations into one bracket; the API
    // only returns standalone-resolvable markers, so the merged bracket must
    // render as prose, not as a broken link.
    const text = "Faith grows [Faith:70, Faith:51] over time.";
    const segments = segmentSummary(text, [{ marker: "[Faith:70]" }, { marker: "[Faith:51]" }]);
    expect(segments).toEqual([{ kind: "text", start: 0, text }]);
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

  it("round-trips: concatenated segments reproduce the input", () => {
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
