"use client";

import { useSelection } from "@/components/library/selection-context";
import type { Collection } from "@/lib/types";
import { useState } from "react";

/**
 * Collections scope picker for /search (Phase 55). A disclosure button opens a
 * checkbox list of the user's collections; ticking one folds its id into the
 * SHARED selection (SelectionProvider), which SearchPanel already includes in
 * the /search-summary POST scope (Phase 49). An empty selection stays "whole
 * library" — the picker only ADDS the same collection_ids the API already
 * intersects with the JWT user's library server-side (it can only shrink the
 * search, never widen it), so this is a pure front-end affordance over the
 * existing scoped-search plumbing.
 *
 * It drives the SAME shared provider that /library writes to (sessionStorage-
 * bridged), so a collection ticked here also shows selected on /library, and
 * "Clear" resets the whole selection on both routes. Mirrors the Collections
 * block of SermonEditor's ScopePopover for markup + a11y, wired to the shared
 * provider instead of the per-sermon scope callbacks.
 */
export function CollectionScopePicker({ collections }: { collections: Collection[] }) {
  const { collectionIds, toggleCollection, clear } = useSelection();
  const [open, setOpen] = useState(false);

  // No collections => nothing to scope to; render nothing (whole library).
  if (collections.length === 0) {
    return null;
  }

  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        data-testid="collection-scope-picker"
        onClick={() => setOpen((prev) => !prev)}
        className="rounded border border-gray-300 bg-white px-2 py-1 text-gray-700 text-sm"
      >
        Collections
      </button>

      {open ? (
        <aside
          aria-label="Scope search to collections"
          className="mt-2 rounded-lg border border-gray-300 bg-gray-50 p-4"
        >
          <div className="mb-2 flex items-baseline justify-between gap-4">
            <h2 className="font-semibold text-sm">Search within collections</h2>
            <button
              type="button"
              onClick={clear}
              className="shrink-0 text-blue-600 text-xs hover:underline"
            >
              Clear
            </button>
          </div>
          <p className="mb-3 text-gray-600 text-xs">
            Tick collections to scope this search to their books. Leave everything unticked to
            search your whole library.
          </p>
          <ul className="space-y-1">
            {collections.map((collection) => (
              <li key={collection.collection_id}>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={collectionIds.includes(collection.collection_id)}
                    onChange={() => toggleCollection(collection.collection_id)}
                  />
                  <span className="min-w-0 truncate">{collection.name}</span>
                  <span className="text-gray-500 text-xs">
                    {`${collection.book_ids.length} ${
                      collection.book_ids.length === 1 ? "book" : "books"
                    }`}
                  </span>
                </label>
              </li>
            ))}
          </ul>
        </aside>
      ) : null}
    </div>
  );
}
