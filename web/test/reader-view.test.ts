import { describe, expect, it } from "vitest";
import {
  ANCHOR_CONTEXT,
  type ChunkRect,
  OFFSET_EPSILON,
  WINDOW_LIMIT,
  appendPlan,
  atBookStart,
  compensatedScrollTop,
  initialWindowPlan,
  mergeWindow,
  parseChunkParam,
  prependPlan,
  reachedEnd,
  savedPositionSnapshot,
  shouldPersist,
  visiblePosition,
} from "../lib/reader-view";
import type { ChunkItem } from "../lib/types";

function makeChunks(indexes: readonly number[]): ChunkItem[] {
  return indexes.map((chunk_index) => ({ chunk_index, content: `c${chunk_index}` }));
}

describe("parseChunkParam", () => {
  it("accepts plain non-negative base-10 integers", () => {
    expect(parseChunkParam("0")).toBe(0);
    expect(parseChunkParam("12")).toBe(12);
    expect(parseChunkParam("007")).toBe(7);
  });

  it("rejects everything that is not a single plain integer string", () => {
    expect(parseChunkParam(undefined)).toBeNull();
    expect(parseChunkParam(["1", "2"])).toBeNull();
    expect(parseChunkParam("")).toBeNull();
    expect(parseChunkParam("-3")).toBeNull();
    expect(parseChunkParam("1.5")).toBeNull();
    expect(parseChunkParam("1e3")).toBeNull();
    expect(parseChunkParam("12abc")).toBeNull();
    expect(parseChunkParam(" 12")).toBeNull();
  });

  it("rejects integers beyond safe range instead of opening at a garbage float", () => {
    expect(parseChunkParam("999999999999999999999999")).toBeNull();
  });
});

describe("initialWindowPlan", () => {
  it("starts at 0 without an anchor", () => {
    expect(initialWindowPlan(null)).toEqual({ start: 0, limit: WINDOW_LIMIT });
  });

  it("starts ANCHOR_CONTEXT above the anchor, clamped to 0", () => {
    expect(initialWindowPlan(50)).toEqual({ start: 50 - ANCHOR_CONTEXT, limit: WINDOW_LIMIT });
    expect(initialWindowPlan(3)).toEqual({ start: 0, limit: WINDOW_LIMIT });
    expect(initialWindowPlan(ANCHOR_CONTEXT)).toEqual({ start: 0, limit: WINDOW_LIMIT });
  });
});

describe("prependPlan", () => {
  it("is null when nothing is loaded or chunk 0 already is", () => {
    expect(prependPlan([])).toBeNull();
    expect(prependPlan(makeChunks([0, 1, 2]))).toBeNull();
  });

  it("requests exactly the gap above the window, never overlapping", () => {
    // First loaded is 100: one full window above, [60, 100).
    expect(prependPlan(makeChunks([100, 101]))).toEqual({ start: 60, limit: WINDOW_LIMIT });
    // First loaded is 25: the partial gap [0, 25) — limit is the gap size.
    expect(prependPlan(makeChunks([25, 26]))).toEqual({ start: 0, limit: 25 });
  });

  it("never plans a limit below 1 (the API 422s on limit < 1)", () => {
    expect(prependPlan(makeChunks([1]))).toEqual({ start: 0, limit: 1 });
  });
});

describe("appendPlan", () => {
  it("is null before anything is loaded", () => {
    expect(appendPlan([])).toBeNull();
  });

  it("starts one past the last loaded chunk", () => {
    expect(appendPlan(makeChunks([38, 39]))).toEqual({ start: 40, limit: WINDOW_LIMIT });
  });
});

describe("mergeWindow", () => {
  it("adopts the incoming window when nothing is loaded", () => {
    const incoming = makeChunks([5, 6]);
    expect(mergeWindow([], incoming, "append")).toEqual(incoming);
  });

  it("appends only rows strictly above the current last (de-dup on overlap)", () => {
    const current = makeChunks([0, 1, 2]);
    const merged = mergeWindow(current, makeChunks([1, 2, 3, 4]), "append");
    expect(merged.map((c) => c.chunk_index)).toEqual([0, 1, 2, 3, 4]);
  });

  it("prepends only rows strictly below the current first (de-dup on overlap)", () => {
    const current = makeChunks([10, 11]);
    const merged = mergeWindow(current, makeChunks([8, 9, 10]), "prepend");
    expect(merged.map((c) => c.chunk_index)).toEqual([8, 9, 10, 11]);
  });

  it("returns the SAME reference when nothing new arrived, so React skips the re-render", () => {
    const current = makeChunks([10, 11]);
    expect(mergeWindow(current, makeChunks([10, 11]), "append")).toBe(current);
    expect(mergeWindow(current, makeChunks([10, 11]), "prepend")).toBe(current);
    expect(mergeWindow(current, [], "append")).toBe(current);
    const empty: ChunkItem[] = [];
    expect(mergeWindow(empty, [], "append")).toBe(empty);
  });
});

describe("reachedEnd / atBookStart", () => {
  it("a short window is the end-of-book signal", () => {
    expect(reachedEnd(WINDOW_LIMIT, WINDOW_LIMIT)).toBe(false);
    expect(reachedEnd(12, WINDOW_LIMIT)).toBe(true);
    expect(reachedEnd(0, WINDOW_LIMIT)).toBe(true);
  });

  it("the book starts once chunk 0 is loaded", () => {
    expect(atBookStart(makeChunks([0, 1]))).toBe(true);
    expect(atBookStart(makeChunks([1, 2]))).toBe(false);
    expect(atBookStart([])).toBe(false);
  });
});

describe("compensatedScrollTop", () => {
  it("moves the scroller down by exactly the prepended height", () => {
    // 4000px of rows inserted above: 1200 -> 5200 keeps the text still.
    expect(compensatedScrollTop(1200, 10_000, 14_000)).toBe(5200);
  });

  it("floors the delta at 0 — a no-op merge must not move the viewport", () => {
    expect(compensatedScrollTop(1200, 10_000, 10_000)).toBe(1200);
    expect(compensatedScrollTop(1200, 10_000, 9_000)).toBe(1200);
  });
});

describe("visiblePosition", () => {
  const rects: ChunkRect[] = [
    { chunk_index: 10, top: -350, height: 200 }, // fully above the viewport
    { chunk_index: 11, top: -150, height: 400 }, // straddles the top edge
    { chunk_index: 12, top: 250, height: 300 },
  ];

  it("picks the chunk straddling the viewport top with the scrolled-past fraction", () => {
    expect(visiblePosition(rects)).toEqual({ chunk_index: 11, offset_ratio: 0.375 });
  });

  it("reports ratio 0 when the first chunk starts below the viewport top", () => {
    expect(visiblePosition([{ chunk_index: 0, top: 80, height: 300 }])).toEqual({
      chunk_index: 0,
      offset_ratio: 0,
    });
  });

  it("clamps to the last chunk at 1.0 when scrolled past everything", () => {
    expect(visiblePosition([{ chunk_index: 7, top: -500, height: 300 }])).toEqual({
      chunk_index: 7,
      offset_ratio: 1,
    });
  });

  it("treats a zero-height chunk as ratio 0 instead of dividing by zero", () => {
    expect(visiblePosition([{ chunk_index: 3, top: 5, height: 0 }])).toEqual({
      chunk_index: 3,
      offset_ratio: 0,
    });
    // A zero-height rect sitting exactly at the top edge counts as scrolled
    // past (bottom is not below the viewport top), so it clamps to 1.0.
    expect(visiblePosition([{ chunk_index: 3, top: 0, height: 0 }])).toEqual({
      chunk_index: 3,
      offset_ratio: 1,
    });
  });

  it("returns null when nothing was measured (never persist garbage)", () => {
    expect(visiblePosition([])).toBeNull();
  });
});

describe("shouldPersist", () => {
  it("always persists the first snapshot", () => {
    expect(shouldPersist(null, { chunk_index: 0, offset_ratio: 0 })).toBe(true);
  });

  it("persists on a chunk change", () => {
    expect(
      shouldPersist({ chunk_index: 4, offset_ratio: 0.9 }, { chunk_index: 5, offset_ratio: 0 }),
    ).toBe(true);
  });

  it("ignores sub-epsilon offset drift within the same chunk", () => {
    const last = { chunk_index: 4, offset_ratio: 0.5 };
    expect(shouldPersist(last, { chunk_index: 4, offset_ratio: 0.5 })).toBe(false);
    expect(shouldPersist(last, { chunk_index: 4, offset_ratio: 0.5 + OFFSET_EPSILON / 2 })).toBe(
      false,
    );
    expect(shouldPersist(last, { chunk_index: 4, offset_ratio: 0.5 + OFFSET_EPSILON })).toBe(true);
  });
});

describe("savedPositionSnapshot", () => {
  it("is null when no position has been saved (chunk_index null)", () => {
    expect(
      savedPositionSnapshot({
        book_id: "b",
        chunk_index: null,
        offset_ratio: null,
        updated_at: null,
      }),
    ).toBeNull();
  });

  it("reads a NULL offset_ratio as the top of the chunk", () => {
    expect(
      savedPositionSnapshot({
        book_id: "b",
        chunk_index: 12,
        offset_ratio: null,
        updated_at: "2026-06-11T00:00:00Z",
      }),
    ).toEqual({ chunk_index: 12, offset_ratio: 0 });
    expect(
      savedPositionSnapshot({
        book_id: "b",
        chunk_index: 12,
        offset_ratio: 0.25,
        updated_at: "2026-06-11T00:00:00Z",
      }),
    ).toEqual({ chunk_index: 12, offset_ratio: 0.25 });
  });
});
