"use client";

import type { Collection } from "@/lib/types";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useId, useRef, useState } from "react";
import { useSelection } from "./selection-context";

/**
 * The /library selection bar (Phase 49). A CLIENT island that reads the shared
 * SelectionProvider and acts on the resolved set: jump to a scoped search, clear
 * the selection, or add the selected books to a collection.
 *
 * EMPTY selection => whole library: the bar shows a plain "Searching all N
 * books" hint so the user knows search is unscoped. With a selection it shows
 * the resolved distinct-book count plus the actions. "Search these" is a Link to
 * `/search` — the selection rides along via sessionStorage (no query string), so
 * SearchPanel folds the same scope into its POST. "Add to collection" POSTs the
 * resolved set to the chosen collection's `/books` proxy (the API clamps to the
 * owner's library) and refreshes the server component on success.
 */
export function SelectionBar({
  collections,
  totalBooks,
}: {
  collections: Collection[];
  totalBooks: number;
}) {
  const { resolved, clear } = useSelection();
  const router = useRouter();
  const selectId = useId();
  const [targetId, setTargetId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Ref mirror of `busy` so the async handler gates on the freshest value
  // without re-creating the callback (the CollectionsPanel/SermonList pattern).
  const busyRef = useRef(false);

  const count = resolved.length;

  const onAdd = useCallback(async (): Promise<void> => {
    if (busyRef.current || targetId === "" || resolved.length === 0) {
      return;
    }
    setError(null);
    busyRef.current = true;
    setBusy(true);
    try {
      const res = await fetch(`/api/collections/${encodeURIComponent(targetId)}/books`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ book_ids: resolved }),
      });
      if (res.ok) {
        setTargetId("");
        router.refresh();
      } else if (res.status === 404) {
        setError("That collection no longer exists.");
      } else {
        setError("Could not add the books. Please try again.");
      }
    } catch {
      setError("Network error. Please try again.");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }, [resolved, router, targetId]);

  if (count === 0) {
    return (
      <p className="text-gray-500 text-sm" data-testid="selection-summary">
        Searching all {totalBooks} {totalBooks === 1 ? "book" : "books"} in your library.
      </p>
    );
  }

  return (
    <div
      aria-label="Selected books"
      data-testid="selection-bar"
      className="flex flex-wrap items-center gap-2 rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-sm"
    >
      <span className="font-medium text-blue-900" data-testid="selection-summary">
        {count} {count === 1 ? "book" : "books"} selected
      </span>
      <Link href="/search" className="rounded bg-black px-3 py-1.5 font-medium text-sm text-white">
        Search these
      </Link>
      <button
        type="button"
        onClick={clear}
        className="rounded border border-gray-300 bg-white px-3 py-1.5 text-gray-700 hover:bg-gray-50"
      >
        Clear
      </button>

      {collections.length > 0 ? (
        <span className="ml-auto flex flex-wrap items-center gap-2">
          <label htmlFor={selectId} className="sr-only">
            Add selected books to collection
          </label>
          <select
            id={selectId}
            value={targetId}
            onChange={(e) => setTargetId(e.target.value)}
            className="rounded border border-gray-300 px-2 py-1 text-sm"
          >
            <option value="">Choose a collection…</option>
            {collections.map((collection) => (
              <option key={collection.collection_id} value={collection.collection_id}>
                {collection.name}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => void onAdd()}
            disabled={busy || targetId === ""}
            className="rounded border border-gray-300 bg-white px-3 py-1.5 text-gray-700 hover:bg-gray-50 disabled:opacity-50"
          >
            Add to collection
          </button>
        </span>
      ) : null}

      {error ? (
        <p role="alert" className="w-full text-red-600 text-sm">
          {error}
        </p>
      ) : null}
    </div>
  );
}
