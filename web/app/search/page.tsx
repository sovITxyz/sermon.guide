import { SelectionProvider } from "@/components/library/selection-context";
import { SearchWorkspace } from "@/components/search/SearchWorkspace";
import {
  UnauthenticatedError,
  getCollections,
  getLibrary,
  getSearchHistory,
} from "@/lib/api-server";
import type { Collection, LibraryBook, SearchHistoryItem } from "@/lib/types";
import { redirect } from "next/navigation";

export default async function SearchPage() {
  let books: LibraryBook[];
  let collections: Collection[];
  let history: SearchHistoryItem[];
  try {
    // All bearer-scoped (token stays on the server). The library count backs the
    // "Searching all N books" scope label; collections resolve a whole-collection
    // selection to its member books for the same label; the recent searches back
    // the "Recent" panel (Phase 51 — server-fetched here, reopened client-side).
    [books, collections, history] = await Promise.all([
      getLibrary(),
      getCollections(),
      getSearchHistory(),
    ]);
  } catch (err) {
    if (err instanceof UnauthenticatedError) {
      redirect("/login?next=/search");
    }
    throw err;
  }

  return (
    <section>
      <h1 className="mb-2 font-semibold text-xl">Search</h1>
      <p className="mb-6 text-gray-600 text-sm">
        Ask a question and get a short grounded summary synthesized from your library, with
        citations back to the passages it drew on.
      </p>
      {/* The selection set on /library rides over via sessionStorage and scopes the search. */}
      <SelectionProvider collections={collections}>
        <SearchWorkspace totalBooks={books.length} collections={collections} history={history} />
      </SelectionProvider>
    </section>
  );
}
