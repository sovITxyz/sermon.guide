import { describe, expect, it } from "vitest";
import {
  EMPTY_PREVIEW_PLACEHOLDER,
  formatHistoryDate,
  historyScopeCount,
  toRecentSearchRow,
  toRecentSearchRows,
} from "../lib/search-history";
import type { SearchHistoryItem } from "../lib/types";

/**
 * Phase 51 — the "Recent" panel's list -> preview mapping. The search-history
 * proxies are read-only (GET/GET/DELETE), so there is no request body to
 * whitelist; the unit contract is the projection from a wire `SearchHistoryItem`
 * into the small `RecentSearchRow` the island renders (preview text, day-only
 * date, scope badge).
 */

function item(overrides: Partial<SearchHistoryItem> = {}): SearchHistoryItem {
  return {
    history_id: "h1",
    query: "How do grace and faith relate?",
    scope_book_ids: [],
    scope_collection_ids: [],
    summary_preview: "Grace is given freely, and faith receives it.",
    created_at: "2026-06-29T13:45:07Z",
    ...overrides,
  };
}

describe("formatHistoryDate", () => {
  it("slices the ISO timestamp to a deterministic YYYY-MM-DD (no timezone parse)", () => {
    expect(formatHistoryDate("2026-06-29T13:45:07Z")).toBe("2026-06-29");
    expect(formatHistoryDate("2026-12-31T23:59:59.123456+00:00")).toBe("2026-12-31");
  });
});

describe("historyScopeCount", () => {
  it("sums book + collection ids; 0 for a whole-library search", () => {
    expect(historyScopeCount({ scope_book_ids: [], scope_collection_ids: [] })).toBe(0);
    expect(historyScopeCount({ scope_book_ids: ["b1", "b2"], scope_collection_ids: ["c1"] })).toBe(
      3,
    );
  });
});

describe("toRecentSearchRow", () => {
  it("projects the wire item into a row (query, trimmed preview, date, scope)", () => {
    expect(toRecentSearchRow(item())).toEqual({
      historyId: "h1",
      query: "How do grace and faith relate?",
      preview: "Grace is given freely, and faith receives it.",
      date: "2026-06-29",
      scopeCount: 0,
      scoped: false,
    });
  });

  it("marks a scoped search and counts its ids", () => {
    const row = toRecentSearchRow(
      item({ scope_book_ids: ["b1", "b2"], scope_collection_ids: ["c1"] }),
    );
    expect(row.scoped).toBe(true);
    expect(row.scopeCount).toBe(3);
  });

  it("falls back to a placeholder when the saved summary preview is empty/whitespace", () => {
    expect(toRecentSearchRow(item({ summary_preview: "" })).preview).toBe(
      EMPTY_PREVIEW_PLACEHOLDER,
    );
    expect(toRecentSearchRow(item({ summary_preview: "   \n  " })).preview).toBe(
      EMPTY_PREVIEW_PLACEHOLDER,
    );
  });

  it("trims surrounding whitespace from a non-empty preview", () => {
    expect(toRecentSearchRow(item({ summary_preview: "  hello  " })).preview).toBe("hello");
  });
});

describe("toRecentSearchRows", () => {
  it("maps a list preserving the API's newest-first order", () => {
    const rows = toRecentSearchRows([
      item({ history_id: "newest", created_at: "2026-06-29T13:00:00Z" }),
      item({ history_id: "older", created_at: "2026-06-28T13:00:00Z" }),
    ]);
    expect(rows.map((r) => r.historyId)).toEqual(["newest", "older"]);
  });

  it("returns an empty array for an empty list", () => {
    expect(toRecentSearchRows([])).toEqual([]);
  });
});
