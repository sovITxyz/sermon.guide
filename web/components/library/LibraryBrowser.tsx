"use client";

import { LibraryTable } from "@/components/LibraryTable";
import { CollectionsPanel } from "@/components/library/CollectionsPanel";
import { SelectionBar } from "@/components/library/SelectionBar";
import { useSelection } from "@/components/library/selection-context";
import type { Collection, LibraryBook } from "@/lib/types";
import { useMemo } from "react";

/**
 * The /library client island (Phase 49). Replaces the page's direct LibraryTable
 * render so the table's checkbox column, the SelectionBar, and the CollectionsPanel
 * all sit under the shared SelectionProvider (mounted by the page). It reads the
 * shared selection and wires the table's per-book checkboxes to `toggleBook`; the
 * table stays presentational. The page remains a server component that fetches
 * `books` + `collections` and passes them down.
 */
export function LibraryBrowser({
  books,
  collections,
}: {
  books: LibraryBook[];
  collections: Collection[];
}) {
  const { bookIds, toggleBook } = useSelection();
  // The checkbox column reflects the AD-HOC ticked books (toggleBook owns these);
  // a Set for O(1) per-row lookup, rebuilt only when the selection changes.
  const selectedBookIds = useMemo(() => new Set(bookIds), [bookIds]);

  return (
    <div className="space-y-6">
      <SelectionBar collections={collections} totalBooks={books.length} />
      <LibraryTable books={books} selectedBookIds={selectedBookIds} onToggle={toggleBook} />
      <CollectionsPanel collections={collections} books={books} />
    </div>
  );
}
