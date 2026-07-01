"use client";

import { SearchPanel } from "@/components/SearchPanel";
import { RecentSearches } from "@/components/search/RecentSearches";
import type {
  Collection,
  SearchHistoryEntry,
  SearchHistoryItem,
  SummaryResponse,
} from "@/lib/types";
import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";

/**
 * Client coordinator for the /search page (Phase 51): lays out SearchPanel
 * beside the "Recent" panel and owns the one piece of state the two islands
 * share — the saved result hydrated when the user reopens a past search.
 *
 * The /search page is a server component, so it cannot hold the hydration state
 * or pass a callback across the two client islands; this thin client parent does
 * (the minimal idiomatic wiring, the same shape as the page wrapping SearchPanel
 * in SelectionProvider). Reopening a recent entry pushes its saved
 * `SummaryResponse` into SearchPanel as `hydratedResult` — a FRESH object each
 * time so the prop identity changes and SearchPanel re-adopts it, even on a
 * repeat reopen. A live search instead calls `onSearched`, which
 * `router.refresh()`es the server component so the freshly-saved row appears in
 * the Recent list (client state — incl. the rendered summary — survives the soft
 * refresh).
 */
export function SearchWorkspace({
  totalBooks,
  collections,
  history,
}: {
  totalBooks: number;
  collections: Collection[];
  history: SearchHistoryItem[];
}) {
  const router = useRouter();
  const [hydrated, setHydrated] = useState<SummaryResponse | null>(null);

  const onOpen = useCallback((entry: SearchHistoryEntry) => {
    // A fresh object so SearchPanel's hydration effect fires even when the same
    // entry is reopened twice (the effect keys on prop identity).
    setHydrated({ summary: entry.result.summary, citations: entry.result.citations });
  }, []);

  const onSearched = useCallback(() => {
    router.refresh();
  }, [router]);

  return (
    <div className="grid gap-8 lg:grid-cols-[1fr_20rem]">
      <SearchPanel
        totalBooks={totalBooks}
        collections={collections}
        hydratedResult={hydrated}
        onSearched={onSearched}
      />
      <aside className="lg:border-gray-100 lg:border-l lg:pl-8">
        <RecentSearches items={history} onOpen={onOpen} />
      </aside>
    </div>
  );
}
