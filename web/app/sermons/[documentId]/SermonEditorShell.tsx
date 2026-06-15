"use client";

import type { DocumentFull } from "@/lib/types";
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
 * doc open) and passes the owned-`book_id` set down as `ownedBookIds`. The
 * editor shares it with every citation node view via context, so the
 * degraded-badge decision ("no longer in your library") costs ZERO per-citation
 * fetches. The set arrives as a string[] from the server component (Sets do not
 * cross the RSC boundary) and is rebuilt into a Set here on the client.
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
  ownedBookIds,
}: {
  document: DocumentFull;
  ownedBookIds: readonly string[];
}) {
  return <SermonEditor document={document} ownedBookIds={new Set(ownedBookIds)} />;
}
