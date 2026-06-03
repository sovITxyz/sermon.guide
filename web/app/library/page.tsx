import { LibraryTable } from "@/components/LibraryTable";
import { UnauthenticatedError, getLibrary } from "@/lib/api-server";
import type { LibraryBook } from "@/lib/types";
import Link from "next/link";
import { redirect } from "next/navigation";

export default async function LibraryPage() {
  let books: LibraryBook[];
  try {
    books = await getLibrary();
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
    </section>
  );
}
