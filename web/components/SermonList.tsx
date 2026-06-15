import type { DocumentListItem } from "@/lib/types";
import Link from "next/link";

/** ISO-8601 → YYYY-MM-DD. Deterministic (no locale/timezone) so the
 * server-rendered date never drifts between environments. Mirrors
 * LibraryTable.formatAdded. */
function formatUpdated(iso: string): string {
  return iso.slice(0, 10);
}

/** Href into the editor for one sermon. */
function sermonHref(documentId: string): string {
  return `/sermons/${encodeURIComponent(documentId)}`;
}

/**
 * The /sermons list. Pure server-rendered component (no client interactivity —
 * the create flow is its own client island). Each row shows the title, a PLAIN
 * TEXT preview of the server-derived `content_text` (the API's `preview` field,
 * never dangerouslySetInnerHTML), and the last-updated date. Empty-state mirrors
 * LibraryTable.
 */
export function SermonList({ documents }: { documents: DocumentListItem[] }) {
  if (documents.length === 0) {
    return (
      <p className="rounded-lg border border-gray-300 border-dashed p-8 text-center text-gray-600 text-sm">
        You have no sermons yet. Create your first one to get started.
      </p>
    );
  }

  return (
    <ul className="divide-y divide-gray-100">
      {documents.map((doc) => (
        <li key={doc.document_id} className="py-3">
          <Link href={sermonHref(doc.document_id)} className="group block">
            <div className="flex items-baseline justify-between gap-4">
              <span className="font-medium text-sm group-hover:underline">{doc.title}</span>
              <span className="shrink-0 text-gray-500 text-xs">
                {formatUpdated(doc.updated_at)}
              </span>
            </div>
            {doc.preview ? (
              <p className="mt-1 line-clamp-2 text-gray-600 text-sm">{doc.preview}</p>
            ) : (
              <p className="mt-1 text-gray-400 text-sm italic">No content yet.</p>
            )}
          </Link>
        </li>
      ))}
    </ul>
  );
}
