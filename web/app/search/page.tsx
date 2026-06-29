import { SearchPanel } from "@/components/SearchPanel";
import { SelectionProvider } from "@/components/library/selection-context";
import { UnauthenticatedError, getCollections, getLibrary } from "@/lib/api-server";
import type { Collection, LibraryBook } from "@/lib/types";
import { redirect } from "next/navigation";

export default async function SearchPage() {
  let books: LibraryBook[];
  let collections: Collection[];
  try {
    // Both bearer-scoped (token stays on the server). The library count backs
    // the "Searching all N books" scope label; collections resolve a whole-
    // collection selection to its member books for the same label.
    [books, collections] = await Promise.all([getLibrary(), getCollections()]);
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
        <SearchPanel totalBooks={books.length} />
      </SelectionProvider>
    </section>
  );
}
