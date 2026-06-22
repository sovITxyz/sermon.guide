"use client";

import type { DocumentFull, EditorLinkStatus, LibraryBookRef } from "@/lib/types";
import dynamic from "next/dynamic";

/**
 * Client shell that DYNAMIC-IMPORTS the TipTap editor (Phase 35). This is the
 * first `next/dynamic` use in web/: code-splitting the ~hundreds-of-kB TipTap +
 * ProseMirror bundle into its own chunk so it loads ONLY when the editor route
 * renders, never on /library, /search, /read, or /upload. `ssr: false` is
 * required twice over — TipTap's `useEditor` needs `immediatelyRender: false`
 * under the App Router and ProseMirror touches the DOM, so the editor is a
 * browser-only component.
 *
 * The server shell (page.tsx) fetches the full document server-side and passes
 * it down, so the editor opens with content already in hand — the dynamic
 * import only defers the editor CODE, not the data.
 *
 * Phase 37: the page also fetches the user's library ONCE (one /library call on
 * doc open) and passes it down as `libraryBooks` ({book_id, title}[] — plain
 * JSON crosses the RSC boundary, a Set does not). This client shell derives two
 * shared lookups from it: the owned-`book_id` Set every citation node view reads
 * via context for the degraded badge (ZERO per-citation fetches), and the
 * `book_id` -> title map the in-editor LibraryDrawer uses to cache `bookTitle`
 * into a citation at insert (a raw /search hit carries no title).
 */
const SermonEditor = dynamic(
  () => import("@/components/SermonEditor").then((mod) => mod.SermonEditor),
  {
    ssr: false,
    loading: () => <p className="text-gray-600 text-sm">Loading the editor…</p>,
  },
);

export function SermonEditorShell({
  document,
  libraryBooks,
  linkStatus,
  googleConnected,
}: {
  document: DocumentFull;
  libraryBooks: readonly LibraryBookRef[];
  // The external-editor link state (Phase 45), fetched server-side on doc open.
  // When `state === "linked"` the editor opens HARD read-only with the "Editing
  // externally" banner; otherwise editable. `web_url` is the only external
  // string and is opened with rel="noopener noreferrer" — NO token/file-id.
  linkStatus: EditorLinkStatus;
  // Whether the user has a Google connection — drives the unlinked editor's
  // "Link to Google Docs" button vs the "Connect Google in Settings" hint.
  googleConnected: boolean;
}) {
  const ownedBookIds = new Set(libraryBooks.map((book) => book.book_id));
  const bookTitles = new Map(libraryBooks.map((book) => [book.book_id, book.title]));
  return (
    <SermonEditor
      document={document}
      ownedBookIds={ownedBookIds}
      bookTitles={bookTitles}
      linkStatus={linkStatus}
      googleConnected={googleConnected}
    />
  );
}
