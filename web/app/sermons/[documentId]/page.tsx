import { SermonEditorShell } from "@/app/sermons/[documentId]/SermonEditorShell";
import { DocumentNotFoundError, UnauthenticatedError, getDocument } from "@/lib/api-server";
import type { DocumentFull } from "@/lib/types";
import Link from "next/link";
import { redirect } from "next/navigation";

interface SermonEditorPageProps {
  params: Promise<{ documentId: string }>;
}

/**
 * Editor server shell (Phase 35, B2 slice B). Fetches the full sermon
 * server-side (the bearer stays on the server — lib/api-server.ts) so the
 * editor opens with content already in hand, then hands the document to the
 * "use client" editor, which is dynamic-imported in SermonEditorShell so the
 * TipTap bundle never loads on non-editor routes.
 *
 * Unauthenticated -> redirect to /login (middleware already gates /sermons,
 * but a server fetch that 401s is the authoritative check). The API's uniform
 * 404 (non-owned / nonexistent / soft-deleted — no existence oracle) renders a
 * not-found state in place rather than redirecting.
 */
export default async function SermonEditorPage({ params }: SermonEditorPageProps) {
  const { documentId } = await params;

  let document: DocumentFull;
  try {
    document = await getDocument(documentId);
  } catch (err) {
    if (err instanceof UnauthenticatedError) {
      redirect(`/login?next=/sermons/${encodeURIComponent(documentId)}`);
    }
    if (err instanceof DocumentNotFoundError) {
      return (
        <section>
          <div className="mb-6 flex items-baseline justify-between gap-4">
            <h1 className="font-semibold text-xl">Sermon not found</h1>
            <Link href="/sermons" className="shrink-0 text-blue-600 text-sm hover:underline">
              ← Sermons
            </Link>
          </div>
          <p className="rounded-lg border border-gray-300 border-dashed p-8 text-center text-gray-600 text-sm">
            This sermon does not exist or is not yours.
          </p>
        </section>
      );
    }
    throw err;
  }

  return <SermonEditorShell document={document} />;
}
