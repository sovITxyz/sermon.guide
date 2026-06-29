import { LibraryTable } from "@/components/LibraryTable";
import { CollectionsPanel } from "@/components/library/CollectionsPanel";
import { UnauthenticatedError, getCollections, getLibrary } from "@/lib/api-server";
import type { Collection, LibraryBook } from "@/lib/types";
import Link from "next/link";
import { redirect } from "next/navigation";

export default async function LibraryPage() {
  let books: LibraryBook[];
  let collections: Collection[];
  try {
    // Server-fetched together (both bearer-scoped, token stays on the server).
    [books, collections] = await Promise.all([getLibrary(), getCollections()]);
  } catch (err) {
    if (err instanceof UnauthenticatedError) {
      redirect("/login?next=/library");
    }
    throw err;
  }

  return (
    <section>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="font-semibold text-xl">Your library</h1>
        <Link href="/upload" className="rounded bg-black px-3 py-2 font-medium text-sm text-white">
          Upload a book
        </Link>
      </div>
      <LibraryTable books={books} />
      <div className="mt-8">
        <CollectionsPanel collections={collections} books={books} />
      </div>
    </section>
  );
}
