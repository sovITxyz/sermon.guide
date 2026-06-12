import { Reader } from "@/components/reader/Reader";
import { UnauthenticatedError, getLibrary } from "@/lib/api-server";
import { parseChunkParam } from "@/lib/reader-view";
import Link from "next/link";
import { redirect } from "next/navigation";

interface ReadPageProps {
  params: Promise<{ bookId: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export default async function ReadPage({ params, searchParams }: ReadPageProps) {
  const { bookId } = await params;
  const anchorChunk = parseChunkParam((await searchParams).chunk);

  // Library lookup is for the header title only — ownership is enforced by
  // the API (the chunks fetch 404s uniformly for non-owned/unknown ids), so a
  // missing row here just means no title, not an early verdict.
  let title: string | null = null;
  try {
    const books = await getLibrary();
    title = books.find((book) => book.book_id === bookId)?.title ?? null;
  } catch (err) {
    if (err instanceof UnauthenticatedError) {
      redirect(`/login?next=/read/${encodeURIComponent(bookId)}`);
    }
    throw err;
  }

  return (
    <section>
      <div className="mb-6 flex items-baseline justify-between gap-4">
        <h1 className="truncate font-semibold text-xl">{title ?? "Read"}</h1>
        <Link href="/library" className="shrink-0 text-blue-600 text-sm hover:underline">
          ← Library
        </Link>
      </div>
      <Reader bookId={bookId} anchorChunk={anchorChunk} />
    </section>
  );
}
