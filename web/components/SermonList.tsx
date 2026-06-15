"use client";

import type { DocumentFull, DocumentListItem } from "@/lib/types";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useRef, useState } from "react";

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
 * A just-deleted sermon, held in the undo toast until the user restores it or
 * the affordance is dismissed. `title` is kept only to label the toast — the
 * restore call needs nothing but the id (POST /{id}/restore is body-less).
 */
interface PendingUndo {
  documentId: string;
  title: string;
}

/**
 * The /sermons list (Phase 36, B2 slice C). A CLIENT island — rows still link
 * into the editor, but each now carries a soft-DELETE action, and a successful
 * delete raises an undo toast that RESTORES the sermon.
 *
 * Restore reachability (recorded in web/AGENTS.md): soft-deleted docs vanish
 * from the default list (the api list is non-deleted only, and there is no
 * "list deleted" endpoint to add this phase — api/ is out of scope here). So
 * restore is reached via an UNDO TOAST shown immediately after a delete, not a
 * separate "recently deleted" view. The toast holds exactly the last delete;
 * a new delete replaces it. After a refresh (or navigating away) the affordance
 * is gone — by design, the toast is the in-session undo window, and the
 * confirm-before-delete prompt is the guard against accidental loss.
 *
 * Mutations route through the same-origin proxies (DELETE /api/documents/[id],
 * POST /api/documents/[id]/restore), then `router.refresh()` re-runs the server
 * component so the list reflects the new state (a server component cannot mutate
 * — the island owns the action + refresh). Each row preview is PLAIN TEXT (the
 * api's `preview` field) — never dangerouslySetInnerHTML.
 */
export function SermonList({ documents }: { documents: DocumentListItem[] }) {
  const router = useRouter();
  // The id currently mid-flight (delete or restore) so its row/toast button
  // disables and we never fire a second mutation over the first.
  const [busyId, setBusyId] = useState<string | null>(null);
  const [undo, setUndo] = useState<PendingUndo | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A ref mirror of busyId so the async handlers gate on the freshest value
  // without re-creating the callbacks on every busy transition.
  const busyRef = useRef<string | null>(null);

  const onDelete = useCallback(
    async (doc: DocumentListItem): Promise<void> => {
      if (busyRef.current !== null) {
        return;
      }
      // A manuscript is irreplaceable — never delete without explicit intent.
      const confirmed = window.confirm(
        `Delete “${doc.title}”? You can undo this right after, before you leave the page.`,
      );
      if (!confirmed) {
        return;
      }
      setError(null);
      busyRef.current = doc.document_id;
      setBusyId(doc.document_id);
      try {
        const res = await fetch(`/api/documents/${encodeURIComponent(doc.document_id)}`, {
          method: "DELETE",
        });
        // 204 on success; the uniform 404 means it is already gone — treat both
        // as "no longer in the list" and refresh. Anything else is a real error.
        if (res.status === 204 || res.status === 404) {
          setUndo({ documentId: doc.document_id, title: doc.title });
          router.refresh();
        } else {
          setError("Could not delete the sermon. Please try again.");
        }
      } catch {
        setError("Network error. Please try again.");
      } finally {
        busyRef.current = null;
        setBusyId(null);
      }
    },
    [router],
  );

  const onRestore = useCallback(async (): Promise<void> => {
    if (busyRef.current !== null || undo === null) {
      return;
    }
    const target = undo;
    setError(null);
    busyRef.current = target.documentId;
    setBusyId(target.documentId);
    try {
      const res = await fetch(`/api/documents/${encodeURIComponent(target.documentId)}/restore`, {
        method: "POST",
      });
      if (res.ok) {
        // The full doc comes back with content intact (api restore clears
        // deleted_at, never touches content); drop the toast and refresh so the
        // row returns to the list.
        (await res.json()) as DocumentFull;
        setUndo(null);
        router.refresh();
      } else {
        setError("Could not restore the sermon. Please try again.");
      }
    } catch {
      setError("Network error. Please try again.");
    } finally {
      busyRef.current = null;
      setBusyId(null);
    }
  }, [router, undo]);

  return (
    <div>
      {error ? (
        <p role="alert" className="mb-3 text-red-600 text-sm">
          {error}
        </p>
      ) : null}

      {documents.length === 0 ? (
        <p className="rounded-lg border border-gray-300 border-dashed p-8 text-center text-gray-600 text-sm">
          You have no sermons yet. Create your first one to get started.
        </p>
      ) : (
        <ul className="divide-y divide-gray-100">
          {documents.map((doc) => (
            <li key={doc.document_id} className="flex items-start justify-between gap-4 py-3">
              <Link href={sermonHref(doc.document_id)} className="group min-w-0 flex-1 block">
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
              <button
                type="button"
                onClick={() => void onDelete(doc)}
                disabled={busyId === doc.document_id}
                aria-label={`Delete ${doc.title}`}
                className="shrink-0 rounded border border-gray-300 px-2 py-1 text-gray-600 text-xs hover:border-red-300 hover:text-red-600 disabled:opacity-50"
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}

      {undo ? (
        // <output> carries an implicit role="status" (a polite live region):
        // the undo affordance is announced WITHOUT stealing the route
        // announcer's role="alert" — a bare getByRole("alert") matches Next's
        // always-present #__next-route-announcer__ (see web/AGENTS.md).
        <output className="mt-4 flex items-center justify-between gap-4 rounded-lg border border-gray-300 bg-gray-50 p-3">
          <span className="text-gray-700 text-sm">Deleted “{undo.title}”.</span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void onRestore()}
              disabled={busyId === undo.documentId}
              className="rounded bg-black px-3 py-1.5 font-medium text-sm text-white disabled:opacity-50"
            >
              Undo
            </button>
            <button
              type="button"
              onClick={() => setUndo(null)}
              aria-label="Dismiss"
              className="rounded px-2 py-1.5 text-gray-500 text-sm hover:text-gray-700"
            >
              Dismiss
            </button>
          </div>
        </output>
      ) : null}
    </div>
  );
}
