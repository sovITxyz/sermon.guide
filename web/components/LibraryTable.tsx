import type { LibraryBook } from "@/lib/types";
import Link from "next/link";

/** ISO-8601 → YYYY-MM-DD. Deterministic (no locale/timezone) so the
 * server-rendered date never drifts between environments. */
function formatAdded(iso: string): string {
  return iso.slice(0, 10);
}

export function LibraryTable({ books }: { books: LibraryBook[] }) {
  if (books.length === 0) {
    return (
      <p className="rounded-lg border border-gray-300 border-dashed p-8 text-center text-gray-600 text-sm">
        Your library is empty.{" "}
        <Link href="/upload" className="text-blue-600 hover:underline">
          Upload a book
        </Link>{" "}
        to get started.
      </p>
    );
  }

  return (
    <table className="w-full border-collapse text-left text-sm">
      <thead>
        <tr className="border-gray-200 border-b text-gray-500">
          <th className="py-2 pr-4 font-medium">Title</th>
          <th className="py-2 pr-4 font-medium">Author</th>
          <th className="py-2 pr-4 font-medium">Category</th>
          <th className="py-2 font-medium">Added</th>
        </tr>
      </thead>
      <tbody>
        {books.map((book) => (
          <tr key={book.book_id} className="border-gray-100 border-b">
            <td className="py-2 pr-4">{book.title}</td>
            <td className="py-2 pr-4 text-gray-600">{book.author ?? "—"}</td>
            <td className="py-2 pr-4 text-gray-600">{book.category ?? "—"}</td>
            <td className="py-2 text-gray-600">{formatAdded(book.added_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
