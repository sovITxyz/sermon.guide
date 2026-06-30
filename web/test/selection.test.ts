import { describe, expect, it } from "vitest";
import { resolveSelection } from "../lib/selection";
import type { Collection } from "../lib/types";

/**
 * resolveSelection (Phase 49) tests. The pure resolver folds the ad-hoc ticked
 * `bookIds` and the whole-collection `collectionIds` into the UNION of distinct
 * book ids the selection covers — the count/label + React-key source for the UI.
 */

function collection(overrides: Partial<Collection> & { collection_id: string }): Collection {
  return {
    name: "A collection",
    description: null,
    created_at: "2026-06-15T00:00:00Z",
    book_ids: [],
    ...overrides,
  };
}

const COLLECTIONS: Collection[] = [
  collection({ collection_id: "c1", book_ids: ["b2", "b3"] }),
  collection({ collection_id: "c2", book_ids: ["b3", "b4"] }),
];

describe("resolveSelection", () => {
  it("returns the ad-hoc book ids when no collection is selected", () => {
    expect(resolveSelection(["b1", "b2"], [], COLLECTIONS)).toEqual(["b1", "b2"]);
  });

  it("unions ad-hoc books with the member books of selected collections", () => {
    // b1 ad-hoc, then c1's members b2,b3.
    expect(resolveSelection(["b1"], ["c1"], COLLECTIONS)).toEqual(["b1", "b2", "b3"]);
  });

  it("dedupes across ad-hoc books and overlapping collections, keeping first-seen order", () => {
    // b3 appears ad-hoc AND in both c1 and c2; b2 appears in c1; b4 only in c2.
    // Order: ad-hoc first (b3), then c1 members (b2,b3->b3 already seen), then c2 (b3,b4).
    expect(resolveSelection(["b3"], ["c1", "c2"], COLLECTIONS)).toEqual(["b3", "b2", "b4"]);
  });

  it("ignores a collection id that names no current collection (stale/deleted)", () => {
    expect(resolveSelection(["b1"], ["does-not-exist"], COLLECTIONS)).toEqual(["b1"]);
  });

  it("ignores an unknown collection while still resolving the known ones", () => {
    expect(resolveSelection([], ["c1", "ghost"], COLLECTIONS)).toEqual(["b2", "b3"]);
  });

  it("returns an empty union for an empty selection (= whole library)", () => {
    expect(resolveSelection([], [], COLLECTIONS)).toEqual([]);
  });

  it("dedupes repeated ad-hoc book ids", () => {
    expect(resolveSelection(["b1", "b1", "b2"], [], COLLECTIONS)).toEqual(["b1", "b2"]);
  });
});
