import type { SearchHistoryItem } from "./types";

/**
 * Pure helpers for the "Recent" search-history panel (Phase 51). No DOM, no
 * server-only imports — unit-tested in test/search-history.test.ts and safe in
 * any bundle.
 *
 * The search-history proxies are READ-ONLY (GET list, GET full, DELETE), so —
 * unlike the calendar/collections proxies — there is no request body to
 * whitelist. The contract this file owns is the LIST -> PREVIEW MAPPING: it
 * projects one wire `SearchHistoryItem` into the small `RecentSearchRow` the
 * RecentSearches island renders, deriving the one-line summary preview, the
 * day-only date, and the scope badge from the lightweight list payload (the
 * heavy `result`/citations blob never reaches the list — it rides only on the
 * per-id GET used to rehydrate the saved render).
 */

/** A just-the-row projection of a saved search for the Recent panel. */
export interface RecentSearchRow {
  historyId: string;
  query: string;
  /** The summary preview, trimmed; a placeholder when the saved summary was empty. */
  preview: string;
  /** Day-only `YYYY-MM-DD` (deterministic — no locale/timezone). */
  date: string;
  /** Total scoped ids (books + collections); 0 means the search ran whole-library. */
  scopeCount: number;
  /** `true` when the search was scoped to a chosen subset (drives the scope badge). */
  scoped: boolean;
}

/** Shown in place of an empty summary preview so a row never renders blank. */
export const EMPTY_PREVIEW_PLACEHOLDER = "No summary preview.";

/**
 * ISO-8601 -> `YYYY-MM-DD`. Deterministic (no `new Date` parse, no
 * locale/timezone) so the server-rendered date never drifts between
 * environments. Mirrors SermonList.formatUpdated / LibraryTable.formatAdded.
 */
export function formatHistoryDate(iso: string): string {
  return iso.slice(0, 10);
}

/**
 * The number of ids the saved search was scoped to (books + collections). A
 * count of 0 is the whole-library (unscoped) search — the empty-selection
 * default carried over from Phase 49.
 */
export function historyScopeCount(
  item: Pick<SearchHistoryItem, "scope_book_ids" | "scope_collection_ids">,
): number {
  return item.scope_book_ids.length + item.scope_collection_ids.length;
}

/**
 * Project one wire list item into the `RecentSearchRow` the panel renders. The
 * preview is the trimmed `summary_preview`, falling back to a placeholder so a
 * saved-but-empty summary still renders a non-blank row.
 */
export function toRecentSearchRow(item: SearchHistoryItem): RecentSearchRow {
  const scopeCount = historyScopeCount(item);
  const trimmed = item.summary_preview.trim();
  return {
    historyId: item.history_id,
    query: item.query,
    preview: trimmed.length > 0 ? trimmed : EMPTY_PREVIEW_PLACEHOLDER,
    date: formatHistoryDate(item.created_at),
    scopeCount,
    scoped: scopeCount > 0,
  };
}

/** Map a whole list payload into rows, preserving the API's newest-first order. */
export function toRecentSearchRows(items: SearchHistoryItem[]): RecentSearchRow[] {
  return items.map(toRecentSearchRow);
}
