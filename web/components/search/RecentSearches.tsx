"use client";

import { toRecentSearchRow } from "@/lib/search-history";
import type { SearchHistoryEntry, SearchHistoryItem } from "@/lib/types";
import { useRouter } from "next/navigation";
import { useCallback, useRef, useState } from "react";

/**
 * The "Recent" search-history panel (Phase 51) — a CLIENT island styled like
 * SermonList. Each row is a saved /search-summary run; clicking it REOPENS the
 * saved result instantly (no re-run of the 2–4 min pipeline): it fetches the
 * full entry from GET /api/search-history/{id} and hands the saved `result` to
 * the parent (`onOpen`) which hydrates SearchPanel's existing summary/citation
 * render. A per-row Delete hits DELETE /api/search-history/{id} then
 * `router.refresh()` to re-run the /search server component against the new
 * state (a server component cannot mutate — the island owns the action +
 * refresh, the same pattern as SermonList).
 *
 * Reopen deliberately does NOT call /search-summary — that is the whole point of
 * saving the full result (the costly path runs once, replays are free). All ids
 * are `encodeURIComponent`'d into the proxy URLs; the query + preview render as
 * PLAIN TEXT — zero `dangerouslySetInnerHTML` (repo invariant).
 */
export function RecentSearches({
  items,
  onOpen,
}: {
  items: SearchHistoryItem[];
  onOpen: (entry: SearchHistoryEntry) => void;
}) {
  const router = useRouter();
  // The id currently mid-flight (open or delete) so its row disables and we
  // never fire a second mutation/fetch over the first.
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const busyRef = useRef<string | null>(null);

  const onSelect = useCallback(
    async (item: SearchHistoryItem): Promise<void> => {
      if (busyRef.current !== null) {
        return;
      }
      setError(null);
      busyRef.current = item.history_id;
      setBusyId(item.history_id);
      try {
        const res = await fetch(`/api/search-history/${encodeURIComponent(item.history_id)}`);
        if (res.ok) {
          const entry = (await res.json()) as SearchHistoryEntry;
          onOpen(entry);
        } else if (res.status === 404) {
          // Already gone (deleted in another tab) — drop the stale row.
          setError("That search is no longer available.");
          router.refresh();
        } else {
          setError("Could not open that search. Please try again.");
        }
      } catch {
        setError("Network error. Please try again.");
      } finally {
        busyRef.current = null;
        setBusyId(null);
      }
    },
    [onOpen, router],
  );

  const onDelete = useCallback(
    async (item: SearchHistoryItem): Promise<void> => {
      if (busyRef.current !== null) {
        return;
      }
      setError(null);
      busyRef.current = item.history_id;
      setBusyId(item.history_id);
      try {
        const res = await fetch(`/api/search-history/${encodeURIComponent(item.history_id)}`, {
          method: "DELETE",
        });
        // 204 on success; the uniform 404 means it is already gone — treat both
        // as "no longer listed" and refresh. Anything else is a real error.
        if (res.status === 204 || res.status === 404) {
          router.refresh();
        } else {
          setError("Could not delete that search. Please try again.");
        }
      } catch {
        setError("Network error. Please try again.");
      } finally {
        busyRef.current = null;
        setBusyId(null);
      }
    },
    [router],
  );

  return (
    <div data-testid="recent-searches">
      <h2 className="mb-3 font-semibold text-sm">Recent searches</h2>

      {error ? (
        <p role="alert" className="mb-3 text-red-600 text-sm">
          {error}
        </p>
      ) : null}

      {items.length === 0 ? (
        <p className="rounded-lg border border-gray-300 border-dashed p-6 text-center text-gray-500 text-xs">
          No recent searches yet. Run a search and it will appear here to reopen instantly.
        </p>
      ) : (
        <ul className="divide-y divide-gray-100">
          {items.map((item) => {
            const row = toRecentSearchRow(item);
            return (
              <li key={row.historyId} className="flex items-start justify-between gap-3 py-3">
                <button
                  type="button"
                  onClick={() => void onSelect(item)}
                  disabled={busyId === row.historyId}
                  className="group min-w-0 flex-1 text-left disabled:opacity-50"
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="truncate font-medium text-sm group-hover:underline">
                      {row.query}
                    </span>
                    <span className="shrink-0 text-gray-500 text-xs">{row.date}</span>
                  </div>
                  <p className="mt-1 line-clamp-2 text-gray-600 text-xs">{row.preview}</p>
                  {row.scoped ? (
                    <span className="mt-1 inline-block rounded bg-gray-100 px-1.5 py-0.5 text-gray-600 text-xs">
                      Scoped to {row.scopeCount} {row.scopeCount === 1 ? "item" : "items"}
                    </span>
                  ) : null}
                </button>
                <button
                  type="button"
                  onClick={() => void onDelete(item)}
                  disabled={busyId === row.historyId}
                  aria-label={`Delete recent search: ${row.query}`}
                  className="shrink-0 rounded border border-gray-300 px-2 py-1 text-gray-600 text-xs hover:border-red-300 hover:text-red-600 disabled:opacity-50"
                >
                  Delete
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
