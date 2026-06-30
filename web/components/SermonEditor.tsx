"use client";

import { today } from "@/lib/dates";
import {
  AUTOSAVE_DEBOUNCE_MS,
  AUTOSAVE_MAX_INTERVAL_MS,
  type EditorSnapshot,
  type FlightState,
  buildPatchBody,
  canKeepaliveFlush,
  idleFlight,
  isDirty,
  onFlightSettled,
  onSaveRequested,
} from "@/lib/sermon-autosave";
import type {
  Collection,
  DocumentFull,
  EditorLinkState,
  EditorLinkStatus,
  ProseMirrorDoc,
  UnlinkMode,
} from "@/lib/types";
import Placeholder from "@tiptap/extension-placeholder";
import { EditorContent, useEditor, useEditorState } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ScheduleSermonPopover } from "./ScheduleSermonPopover";
import { CitationNode } from "./editor/CitationNode";
import { LibraryDrawer } from "./editor/LibraryDrawer";
import { LibraryMembershipProvider } from "./editor/library-membership";

/**
 * Manuscript editor (Phase 36, B2 slice C; citations Phase 37, B2 slice D). A
 * headless TipTap contenteditable with a fixed toolbar, an editable title, and
 * AUTOSAVE — the editor stops losing work. Cited library passages are
 * first-class blocks (the `citation` node, components/editor/CitationNode.tsx).
 *
 * Bundling: dynamic-imported with `ssr: false` from the route shell
 * (SermonEditorShell), so TipTap loads only on the editor route. `useEditor`
 * runs `immediatelyRender: false` per the App Router SSR requirement.
 *
 * Autosave (mirrors the Phase 33 reader position-persistence pattern in
 * lib/reader-view.ts; pure decisions in lib/sermon-autosave.ts):
 *  - 2 s debounce after the last edit + a 15 s max-interval ceiling, so a user
 *    typing without pause still gets saved.
 *  - ONE in-flight PATCH at a time: edits during a flight are COALESCED into one
 *    trailing save fired after it resolves — never parallel PATCHes (parallel
 *    writes race base_updated_at and manufacture 409s).
 *  - A dirty check (isDirty) so an unchanged buffer never PATCHes.
 *  - After every 200, adopt the response `updated_at` as the next
 *    base_updated_at (reusing the stale load value manufactures 409s).
 *  - pagehide flush via fetch keepalive, ONLY when dirty AND the serialized body
 *    is within the ~64 KB keepalive ceiling; an oversize doc SKIPS the flush
 *    (it saves on next open) instead of throwing.
 *  - On 409: status=conflict, STOP the autosave loop, show a banner offering
 *    "Reload latest" (re-GET the doc, reset editor + base_updated_at, resume).
 *    The user's buffer is KEPT until they choose — never auto-clobbered.
 *
 * Save: PATCH {title, content: editor.getJSON(), base_updated_at} through the
 * same-origin /api/documents/[id] proxy, which whitelists exactly those three
 * fields (lib/documents.ts).
 *
 * Security: TipTap is headless contenteditable — ZERO dangerouslySetInnerHTML.
 * The editor renders its own DOM from the ProseMirror document; we never inject
 * raw HTML, and the content round-trips as JSON, not markup.
 */

type SaveStatus = "saved" | "saving" | "error" | "conflict" | "unsaved";

/** A safe default for a downloaded export when the header has no usable name. */
const DEFAULT_EXPORT_FILENAME = "sermon.docx";

/**
 * Recover the download filename from a Content-Disposition header (the export
 * proxy forwards the API's already-sanitized `filename="…"`). Strips any path
 * separators as defense-in-depth and falls back to a safe constant when the
 * header is absent or unparseable — the browser only uses this for the saved
 * file name, never for a request, so a bad value cannot escape the download.
 */
function filenameFromDisposition(disposition: string | null): string {
  if (!disposition) {
    return DEFAULT_EXPORT_FILENAME;
  }
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
  const raw = match?.[1];
  if (!raw) {
    return DEFAULT_EXPORT_FILENAME;
  }
  // Drop any path segments a crafted header might carry — keep only the basename.
  const base = raw.split(/[\\/]/).pop() ?? "";
  const trimmed = base.trim();
  return trimmed.length > 0 ? trimmed : DEFAULT_EXPORT_FILENAME;
}

/**
 * Pull a human-readable message out of a non-OK JSON response without leaking
 * internals — reads FastAPI's `{detail}` (or a proxy `{error}`) and falls back
 * to a generic string on a non-JSON body. Used by the Phase 45 link/pull/unlink
 * handlers so the API's canonical 409/400/502 detail surfaces in the link
 * banner exactly like the docx round-trip does.
 */
async function readErrorMessage(res: Response, fallback: string): Promise<string> {
  try {
    const data = (await res.json()) as { detail?: unknown; error?: unknown };
    const detail = typeof data.detail === "string" ? data.detail : null;
    const error = typeof data.error === "string" ? data.error : null;
    return detail ?? error ?? fallback;
  } catch {
    return fallback;
  }
}

/**
 * Defense-in-depth on the Drive `web_url` before it becomes an `href`. The API
 * returns the Drive `webViewLink` (always an `https://docs.google.com/...` URL),
 * but the editor never trusts a stored string blindly: only an absolute
 * `https:` URL is rendered as a link, so a tampered/`javascript:`/`data:` value
 * can never become a clickable XSS vector. Returns null for anything else, which
 * simply hides the Open affordance.
 */
function safeWebUrl(value: string | null): string | null {
  if (value === null) {
    return null;
  }
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" ? value : null;
  } catch {
    return null;
  }
}

const CONFLICT_MESSAGE =
  "This sermon was changed in another tab or device since you opened it. " +
  "Autosave is paused so neither side is clobbered. Your edits here are safe — " +
  "copy anything you need, then reload to get the latest version.";

/** Build the StarterKit + Placeholder + Citation extension set. StarterKit's
 * own Link is disabled (no link UI in the toolbar); the only interactive links
 * are inside the citation node view. The `citation` node (Phase 37) MUST be in
 * this list so a stored doc containing it parses on load and round-trips through
 * getJSON()/setContent. */
function buildExtensions() {
  return [
    StarterKit.configure({ link: false }),
    Placeholder.configure({ placeholder: "Start writing your sermon…" }),
    CitationNode,
  ];
}

export function SermonEditor({
  document: initialDocument,
  ownedBookIds,
  bookTitles,
  collections = [],
  scopeBookIds: initialScopeBookIds = [],
  scopeCollectionIds: initialScopeCollectionIds = [],
  linkStatus,
  googleConnected = false,
}: {
  document: DocumentFull;
  // The user's owned-`book_id` set, resolved ONCE by the shell's single
  // /library fetch on doc open and shared with every citation node view via
  // context — so the degraded badge costs ZERO per-citation fetches. Defaults to
  // an empty set (everything degraded) when the shell does not supply one.
  ownedBookIds?: ReadonlySet<string>;
  // The {book_id -> title} map from the SAME one-shot /library fetch — the only
  // source of a citation's title (a raw /search hit carries none). Used by the
  // in-editor LibraryDrawer to cache `bookTitle` at insert, AND to label the
  // Scope control's per-book checkboxes (Phase 50). Empty by default.
  bookTitles?: ReadonlyMap<string, string>;
  // The user's library collections (Phase 50), fetched server-side on doc open.
  // Backs the Scope control's collection checkboxes and prunes stale (deleted)
  // collection ids out of the scope handed to the citation drawer's search.
  collections?: Collection[];
  // The per-sermon citation scope (Phase 50), derived by the shell from the
  // document's stored `scope_book_ids` / `scope_collection_ids` (no extra fetch).
  // Empty arrays = whole library. Persisted via the existing autosave path.
  scopeBookIds?: string[];
  scopeCollectionIds?: string[];
  // The external-editor link state (Phase 45), fetched server-side on doc open.
  // When `state === "linked"` the editor is HARD read-only with the "Editing
  // externally" banner; otherwise editable. Defaults to unlinked when omitted.
  linkStatus?: EditorLinkStatus;
  // Whether the user has a Google connection — drives the unlinked editor's
  // "Link to Google Docs" button vs the "Connect Google in Settings" hint.
  googleConnected?: boolean;
}) {
  const [title, setTitle] = useState(initialDocument.title);
  const [status, setStatus] = useState<SaveStatus>("saved");
  // --- external-editor link (Phase 45) -------------------------------------
  // The live link state. Starts from the server-fetched status and is advanced
  // by link/pull/unlink. While `linked` the editor is HARD read-only and
  // autosave is suppressed (linkedRef below gates the loop just like conflict).
  const [linkState, setLinkState] = useState<EditorLinkState>(linkStatus?.state ?? "unlinked");
  const [webUrl, setWebUrl] = useState<string | null>(linkStatus?.web_url ?? null);
  const [remoteChanged, setRemoteChanged] = useState<boolean>(linkStatus?.remote_changed ?? false);
  // A visible, dismissable message for a link/pull/unlink failure (the API's
  // 4xx/409/502 surfaces here), distinct from the save status and the docx
  // banner. A busy flag disables the link affordances during a round-trip.
  const [linkError, setLinkError] = useState<string | null>(null);
  const [linkBusy, setLinkBusy] = useState(false);
  // The unlink choice dialog (the settled pull-final-vs-keep-app mandatory
  // choice). Closed by default; opened by the banner's Unlink button.
  const [unlinkDialogOpen, setUnlinkDialogOpen] = useState(false);
  const isLinked = linkState === "linked";
  // Only an https URL is ever rendered as the Open href (defense-in-depth).
  const safeUrl = safeWebUrl(webUrl);
  // The LibraryDrawer is opened from a toolbar affordance; closed by default so
  // the editor opens uncluttered.
  const [drawerOpen, setDrawerOpen] = useState(false);
  // Per-sermon citation scope (Phase 50). The books / collections the citation
  // drawer is limited to. Held as state for the Scope-control UI AND mirrored
  // into refs below so the autosave loop (which reads refs, never render
  // closures) sees the current scope when it builds a PATCH. The Scope popover
  // is closed by default like the drawer.
  const [scopeOpen, setScopeOpen] = useState(false);
  const [scopeBookIds, setScopeBookIds] = useState<string[]>(initialScopeBookIds);
  const [scopeCollectionIds, setScopeCollectionIds] = useState<string[]>(initialScopeCollectionIds);
  // Schedule-on-calendar (Phase 47): the popover open flag, and the date of the
  // most recently scheduled event — non-null shows a confirmation linking to the
  // calendar. Distinct from the save/link/docx state so a successful schedule
  // never masquerades as a save.
  const [scheduleOpen, setScheduleOpen] = useState(false);
  const [scheduledDate, setScheduledDate] = useState<string | null>(null);
  // DOCX round-trip (Phase 43) UI state: a visible, dismissable message for an
  // export/import failure (the API's 404/413/415/502 surfaces here), and a busy
  // flag that disables both buttons + the file picker during a round-trip so a
  // user cannot fire overlapping imports. Distinct from the autosave SaveStatus
  // because a failed export/import must NOT masquerade as a failed save.
  const [docxError, setDocxError] = useState<string | null>(null);
  const [docxBusy, setDocxBusy] = useState(false);
  // The hidden <input type=file> the Import button proxies its click to (the
  // Uploader sr-only-input pattern). Reset to "" after each pick so re-choosing
  // the same file re-fires onChange.
  const importInputRef = useRef<HTMLInputElement>(null);

  // Stabilize the membership set so a re-render does not hand the provider a new
  // reference (and re-render every node view). The empty-set fallback keeps the
  // degraded path safe when no set is supplied.
  const membership = useMemo(() => ownedBookIds ?? new Set<string>(), [ownedBookIds]);
  // Stabilize the title map likewise so the drawer never re-renders on a new
  // empty-map reference.
  const titleMap = useMemo(() => bookTitles ?? new Map<string, string>(), [bookTitles]);

  // --- autosave machinery (refs so the loop reads fresh values, never stale
  //     closures) -----------------------------------------------------------
  const documentId = initialDocument.document_id;
  // The optimistic-concurrency token. Starts at the loaded `updated_at` and is
  // advanced to the server's returned `updated_at` after every 200 save, so a
  // later save from the same tab is never a false self-conflict.
  const baseUpdatedAt = useRef(initialDocument.updated_at);
  // The last snapshot the server has accepted — the dirty-check baseline.
  const lastSaved = useRef<EditorSnapshot>({
    title: initialDocument.title,
    content: initialDocument.content,
    scopeBookIds: initialScopeBookIds,
    scopeCollectionIds: initialScopeCollectionIds,
  });
  // Latest title kept in a ref so the autosave loop (and the pagehide flush, a
  // non-React event) reads the current value, not a stale render closure.
  const titleRef = useRef(initialDocument.title);
  // The current citation scope mirrored into refs for the same reason — the
  // autosave loop reads these (not the render-closure state) when it builds the
  // PATCH body, so a Scope toggle that just fired the debounce saves correctly.
  const scopeBookIdsRef = useRef(scopeBookIds);
  const scopeCollectionIdsRef = useRef(scopeCollectionIds);
  const flight = useRef<FlightState>(idleFlight());
  // Once a 409 lands, the loop STOPS until the user reloads. Guards every
  // scheduler entry point (debounce, max-interval, trailing) so a conflicted
  // tab never silently re-PATCHes over the other side.
  const conflicted = useRef(false);
  // While a doc is LINKED to an external editor the autosave loop is hard-
  // suppressed (a stale PATCH would clobber the linked source-of-truth). The ref
  // mirrors `isLinked` so the loop — which reads refs, not render state — and the
  // non-React pagehide flush both see the current value without a stale closure.
  const linked = useRef(linkStatus?.state === "linked");
  const debounceTimer = useRef<number | null>(null);
  const maxIntervalTimer = useRef<number | null>(null);
  const mounted = useRef(true);

  const editor = useEditor({
    immediatelyRender: false,
    extensions: buildExtensions(),
    content: initialDocument.content,
    editorProps: {
      attributes: {
        class:
          "prose prose-sm max-w-none min-h-[24rem] rounded-lg border border-gray-300 p-4 focus:outline-none focus:ring-2 focus:ring-black/20",
      },
    },
  });

  // Read the current editor buffer as a snapshot, or null before the editor is
  // ready. Title comes from the ref so a flush during teardown still sees it.
  const readSnapshot = useCallback((): EditorSnapshot | null => {
    if (!editor) {
      return null;
    }
    return {
      title: titleRef.current,
      content: editor.getJSON() as ProseMirrorDoc,
      scopeBookIds: scopeBookIdsRef.current,
      scopeCollectionIds: scopeCollectionIdsRef.current,
    };
  }, [editor]);

  const clearTimers = useCallback((): void => {
    if (debounceTimer.current !== null) {
      window.clearTimeout(debounceTimer.current);
      debounceTimer.current = null;
    }
    if (maxIntervalTimer.current !== null) {
      window.clearTimeout(maxIntervalTimer.current);
      maxIntervalTimer.current = null;
    }
  }, []);

  // Adopt a scope into both the render state and the autosave-loop refs at once
  // (Phase 50). Used by the reload/import/pull reset points so the scope tracks
  // the server's returned document, and by the toggles below.
  const syncScope = useCallback((bookIds: string[], collectionIds: string[]): void => {
    scopeBookIdsRef.current = bookIds;
    scopeCollectionIdsRef.current = collectionIds;
    setScopeBookIds(bookIds);
    setScopeCollectionIds(collectionIds);
  }, []);

  // The actual PATCH. Single-flight is enforced by the caller (runSave) via the
  // flight state machine; this only fires the request and routes the response.
  const sendPatch = useCallback(
    async (snapshot: EditorSnapshot): Promise<void> => {
      setStatus("saving");
      const body = buildPatchBody(snapshot, baseUpdatedAt.current);
      try {
        const res = await fetch(`/api/documents/${encodeURIComponent(documentId)}`, {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!mounted.current) {
          return;
        }
        if (res.status === 409) {
          // STOP the loop; keep the buffer. The conflict banner owns recovery.
          conflicted.current = true;
          clearTimers();
          setStatus("conflict");
          return;
        }
        if (res.status === 413) {
          setStatus("error");
          return;
        }
        if (res.status === 404) {
          setStatus("error");
          return;
        }
        if (!res.ok) {
          setStatus("error");
          return;
        }
        const saved = (await res.json()) as DocumentFull;
        if (!mounted.current) {
          return;
        }
        // Adopt the server's new updated_at and accepted buffer so the next
        // save is neither a false self-conflict nor a redundant re-PATCH.
        baseUpdatedAt.current = saved.updated_at;
        lastSaved.current = snapshot;
        setStatus("saved");
      } catch {
        if (mounted.current) {
          setStatus("error");
        }
      }
    },
    [documentId, clearTimers],
  );

  // Drive one save through the single-flight gate, draining any trailing edit
  // that arrived while a PATCH was in flight. Coalesced edits become exactly
  // ONE more save — never parallel requests.
  const runSave = useCallback((): void => {
    if (conflicted.current || linked.current) {
      return;
    }
    const snapshot = readSnapshot();
    if (!snapshot || !isDirty(lastSaved.current, snapshot)) {
      return;
    }
    const decision = onSaveRequested(flight.current);
    flight.current = decision.state;
    if (!decision.start) {
      // A PATCH is already in flight; this edit is now coalesced into the
      // pending trailing save. Nothing to start here.
      return;
    }
    void sendPatch(snapshot).finally(() => {
      const settled = onFlightSettled(flight.current);
      flight.current = settled.state;
      if (settled.fireTrailing && mounted.current && !conflicted.current) {
        // Exactly one trailing save for the edits coalesced during the flight.
        runSave();
      }
    });
  }, [readSnapshot, sendPatch]);

  // Called on every edit (editor onUpdate + title change). Resets the 2 s
  // debounce and, on the first dirty edit since the last save, arms the 15 s
  // max-interval ceiling so continuous typing still saves.
  const scheduleAutosave = useCallback((): void => {
    if (conflicted.current || linked.current) {
      return;
    }
    setStatus("unsaved");
    if (debounceTimer.current !== null) {
      window.clearTimeout(debounceTimer.current);
    }
    debounceTimer.current = window.setTimeout(() => {
      debounceTimer.current = null;
      if (maxIntervalTimer.current !== null) {
        window.clearTimeout(maxIntervalTimer.current);
        maxIntervalTimer.current = null;
      }
      runSave();
    }, AUTOSAVE_DEBOUNCE_MS);
    if (maxIntervalTimer.current === null) {
      maxIntervalTimer.current = window.setTimeout(() => {
        maxIntervalTimer.current = null;
        // The max-interval save also clears the pending debounce — one save,
        // not two — and the debounce re-arms on the next keystroke.
        if (debounceTimer.current !== null) {
          window.clearTimeout(debounceTimer.current);
          debounceTimer.current = null;
        }
        runSave();
      }, AUTOSAVE_MAX_INTERVAL_MS);
    }
  }, [runSave]);

  // Wire the editor's content-change event to the scheduler. useEffect (not the
  // useEditor options) so the handler closes over the current callbacks.
  useEffect(() => {
    if (!editor) {
      return;
    }
    const onUpdate = (): void => scheduleAutosave();
    editor.on("update", onUpdate);
    return () => {
      editor.off("update", onUpdate);
    };
  }, [editor, scheduleAutosave]);

  // --- citation scope (Phase 50) -------------------------------------------
  // Toggling a book / collection in the Scope popover updates the ref (so a
  // pending autosave reads the new value) and the state (so the popover
  // re-renders), then schedules an autosave — the scope rides the SAME debounce
  // + single-flight path as the manuscript, so no separate request is needed.
  const toggleScopeBook = useCallback(
    (bookId: string): void => {
      const prev = scopeBookIdsRef.current;
      const next = prev.includes(bookId) ? prev.filter((id) => id !== bookId) : [...prev, bookId];
      syncScope(next, scopeCollectionIdsRef.current);
      scheduleAutosave();
    },
    [syncScope, scheduleAutosave],
  );
  const toggleScopeCollection = useCallback(
    (collectionId: string): void => {
      const prev = scopeCollectionIdsRef.current;
      const next = prev.includes(collectionId)
        ? prev.filter((id) => id !== collectionId)
        : [...prev, collectionId];
      syncScope(scopeBookIdsRef.current, next);
      scheduleAutosave();
    },
    [syncScope, scheduleAutosave],
  );

  // The scope handed to the citation drawer's /search. PRUNE stale collection
  // ids: a collection the user deleted (so it is no longer in `collections`)
  // would otherwise reach /search and trip the API's no-oracle 404, failing
  // EVERY citation search. The persisted scope is left intact — the API re-clamps
  // it on the next save — so a transient collections-fetch miss never wipes it.
  const liveCollectionIds = useMemo(
    () => new Set(collections.map((c) => c.collection_id)),
    [collections],
  );
  const drawerScope = useMemo(
    () => ({
      book_ids: scopeBookIds,
      collection_ids: scopeCollectionIds.filter((id) => liveCollectionIds.has(id)),
    }),
    [scopeBookIds, scopeCollectionIds, liveCollectionIds],
  );
  const scopeCount = scopeBookIds.length + drawerScope.collection_ids.length;

  // The read-only lock (Phase 45). While LINKED the editor is HARD read-only —
  // setEditable(false) disables the contenteditable AND the linked ref gates the
  // autosave loop so NO PATCH ever fires while the native Doc is the source of
  // truth. When the link is cleared (unlink / keep-app) editability + autosave
  // resume. The ref is kept in sync here (not only at construction) so a runtime
  // link/unlink flips the lock immediately. Clearing any pending unsaved timers
  // on lock prevents a queued save from firing after the lock engages.
  useEffect(() => {
    linked.current = isLinked;
    if (!editor) {
      return;
    }
    editor.setEditable(!isLinked);
    if (isLinked) {
      clearTimers();
      // A buffer that was mid-edit when the lock engaged settles visually to
      // "saved" — the linked Doc owns the content now, no local save is pending.
      setStatus("saved");
    }
  }, [editor, isLinked, clearTimers]);

  // pagehide flush via fetch keepalive: only when dirty AND within the ~64 KB
  // keepalive ceiling. Oversize -> SKIP silently (next-open save covers it).
  // Also flushes on unmount (SPA navigations never fire pagehide).
  useEffect(() => {
    mounted.current = true;
    const flush = (): void => {
      if (conflicted.current || linked.current) {
        return;
      }
      const snapshot = readSnapshot();
      if (!snapshot || !canKeepaliveFlush(lastSaved.current, snapshot, baseUpdatedAt.current)) {
        return;
      }
      const body = buildPatchBody(snapshot, baseUpdatedAt.current);
      fetch(`/api/documents/${encodeURIComponent(documentId)}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
        keepalive: true,
      }).catch(() => {
        // Best-effort: the page is going away; the next open save covers it.
      });
    };
    window.addEventListener("pagehide", flush);
    return () => {
      window.removeEventListener("pagehide", flush);
      mounted.current = false;
      clearTimers();
      // SPA navigations never fire pagehide — flush on teardown too.
      flush();
    };
  }, [documentId, readSnapshot, clearTimers]);

  // Conflict recovery: re-GET the latest doc and reset the editor, title,
  // base_updated_at, and dirty baseline, then resume autosaving. The user's
  // edits are discarded ONLY here, by their explicit choice.
  const onReloadLatest = useCallback(async (): Promise<void> => {
    if (!editor) {
      return;
    }
    setStatus("saving");
    try {
      const res = await fetch(`/api/documents/${encodeURIComponent(documentId)}`, {
        cache: "no-store",
      });
      if (!res.ok) {
        setStatus("conflict");
        return;
      }
      const latest = (await res.json()) as DocumentFull;
      editor.commands.setContent(latest.content);
      setTitle(latest.title);
      titleRef.current = latest.title;
      baseUpdatedAt.current = latest.updated_at;
      syncScope(latest.scope_book_ids, latest.scope_collection_ids);
      lastSaved.current = {
        title: latest.title,
        content: latest.content,
        scopeBookIds: latest.scope_book_ids,
        scopeCollectionIds: latest.scope_collection_ids,
      };
      flight.current = idleFlight();
      conflicted.current = false;
      setStatus("saved");
    } catch {
      setStatus("conflict");
    }
  }, [editor, documentId, syncScope]);

  // --- DOCX round-trip (Phase 43) ------------------------------------------
  // Download .docx: fetch the export proxy as a Blob and trigger a browser
  // download via an object URL, so a 404/502 surfaces as a VISIBLE message
  // instead of navigating the page away from the editor (a plain <a href> to the
  // proxy would render the error JSON as a page). The proxy forwards the API's
  // sanitized Content-Disposition; we recover the filename from it (with a safe
  // fallback) for the saved file.
  const onExport = useCallback(async (): Promise<void> => {
    setDocxError(null);
    setDocxBusy(true);
    try {
      const res = await fetch(`/api/documents/${encodeURIComponent(documentId)}/export`, {
        cache: "no-store",
      });
      if (!res.ok) {
        let message = "Could not export the sermon.";
        try {
          const data = (await res.json()) as { detail?: unknown; error?: unknown };
          const detail = typeof data.detail === "string" ? data.detail : null;
          const error = typeof data.error === "string" ? data.error : null;
          message = detail ?? error ?? message;
        } catch {
          // Non-JSON body — keep the generic message.
        }
        setDocxError(message);
        return;
      }
      const blob = await res.blob();
      const filename = filenameFromDisposition(res.headers.get("content-disposition"));
      const url = URL.createObjectURL(blob);
      try {
        const anchor = window.document.createElement("a");
        anchor.href = url;
        anchor.download = filename;
        window.document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
      } finally {
        URL.revokeObjectURL(url);
      }
    } catch {
      setDocxError("Could not export the sermon.");
    } finally {
      setDocxBusy(false);
    }
  }, [documentId]);

  // Import .docx: POST the chosen file through the import proxy. On success the
  // API has already SNAPSHOTTED the prior content and OVERWRITTEN the doc, and
  // returns the full updated document — adopt it into the editor exactly like a
  // conflict reload (setContent + title + base_updated_at + dirty baseline), so
  // the editor renders the imported content as TipTap JSON (ZERO
  // dangerouslySetInnerHTML). On a 4xx/404/502 show the API's reason. Clears any
  // paused-conflict state because the buffer is now the freshly-imported doc.
  const onImportFile = useCallback(
    async (file: File): Promise<void> => {
      if (!editor) {
        return;
      }
      setDocxError(null);
      setDocxBusy(true);
      try {
        const body = new FormData();
        body.append("file", file);
        const res = await fetch(`/api/documents/${encodeURIComponent(documentId)}/import`, {
          method: "POST",
          body,
          cache: "no-store",
        });
        if (!res.ok) {
          let message = "Could not import the document.";
          try {
            const data = (await res.json()) as { detail?: unknown; error?: unknown };
            const detail = typeof data.detail === "string" ? data.detail : null;
            const error = typeof data.error === "string" ? data.error : null;
            message = detail ?? error ?? message;
          } catch {
            // Non-JSON body — keep the generic message.
          }
          setDocxError(message);
          return;
        }
        const imported = (await res.json()) as DocumentFull;
        editor.commands.setContent(imported.content);
        setTitle(imported.title);
        titleRef.current = imported.title;
        baseUpdatedAt.current = imported.updated_at;
        syncScope(imported.scope_book_ids, imported.scope_collection_ids);
        lastSaved.current = {
          title: imported.title,
          content: imported.content,
          scopeBookIds: imported.scope_book_ids,
          scopeCollectionIds: imported.scope_collection_ids,
        };
        flight.current = idleFlight();
        conflicted.current = false;
        setStatus("saved");
      } catch {
        setDocxError("Could not import the document.");
      } finally {
        setDocxBusy(false);
      }
    },
    [editor, documentId, syncScope],
  );

  // --- external-editor link actions (Phase 45) -----------------------------
  // Adopt a freshly-pulled document into the editor (mirrors the import/conflict
  // reload: setContent + title + base_updated_at + dirty baseline). The pull
  // ran while LINKED, so the editor stays read-only after — this only refreshes
  // the displayed buffer with the latest Doc content.
  const adoptPulledDoc = useCallback(
    (doc: DocumentFull): void => {
      if (!editor) {
        return;
      }
      editor.commands.setContent(doc.content);
      setTitle(doc.title);
      titleRef.current = doc.title;
      baseUpdatedAt.current = doc.updated_at;
      syncScope(doc.scope_book_ids, doc.scope_collection_ids);
      lastSaved.current = {
        title: doc.title,
        content: doc.content,
        scopeBookIds: doc.scope_book_ids,
        scopeCollectionIds: doc.scope_collection_ids,
      };
      flight.current = idleFlight();
    },
    [editor, syncScope],
  );

  // Link: POST the link proxy. On success the API created the Drive Doc and
  // returns {state, web_url, remote_changed} — flip to read-only linked mode.
  // A 409 (already linked) / 400 (connect Google first) / 502 surfaces in the
  // link banner. Read-only mode is engaged purely from the returned state via
  // the useEffect lock above.
  const onLink = useCallback(async (): Promise<void> => {
    setLinkError(null);
    setLinkBusy(true);
    try {
      const res = await fetch(`/api/documents/${encodeURIComponent(documentId)}/editor-link`, {
        method: "POST",
        cache: "no-store",
      });
      if (!res.ok) {
        setLinkError(await readErrorMessage(res, "Could not link to Google Docs."));
        return;
      }
      const data = (await res.json()) as EditorLinkStatus;
      setLinkState(data.state);
      setWebUrl(data.web_url);
      setRemoteChanged(data.remote_changed);
    } catch {
      setLinkError("Could not link to Google Docs.");
    } finally {
      setLinkBusy(false);
    }
  }, [documentId]);

  // Pull changes: POST the pull proxy. The API snapshots-then-overwrites in one
  // transaction and returns the full updated document; adopt it into the editor
  // buffer (it reloads as TipTap JSON, ZERO dangerouslySetInnerHTML). Clears the
  // remote-changed hint on success.
  const onPull = useCallback(async (): Promise<void> => {
    setLinkError(null);
    setLinkBusy(true);
    try {
      const res = await fetch(`/api/documents/${encodeURIComponent(documentId)}/editor-link/pull`, {
        method: "POST",
        cache: "no-store",
      });
      if (!res.ok) {
        setLinkError(await readErrorMessage(res, "Could not pull changes from Google Docs."));
        return;
      }
      const data = (await res.json()) as DocumentFull;
      adoptPulledDoc(data);
      setRemoteChanged(false);
    } catch {
      setLinkError("Could not pull changes from Google Docs.");
    } finally {
      setLinkBusy(false);
    }
  }, [documentId, adoptPulledDoc]);

  // Unlink with the settled mandatory choice. `pull-final` runs the pull pipeline
  // once (snapshot+overwrite) THEN unlinks — so the app keeps the latest Doc
  // content; `keep-app` leaves the app content untouched and unlinks. On success
  // the doc returns to editable mode (the useEffect lock releases on state
  // change). The proxy whitelists ONLY {mode}.
  const onUnlink = useCallback(
    async (mode: UnlinkMode): Promise<void> => {
      setLinkError(null);
      setLinkBusy(true);
      try {
        const res = await fetch(
          `/api/documents/${encodeURIComponent(documentId)}/editor-link/unlink`,
          {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ mode }),
            cache: "no-store",
          },
        );
        if (!res.ok) {
          setLinkError(await readErrorMessage(res, "Could not unlink from Google Docs."));
          return;
        }
        const data = (await res.json()) as EditorLinkStatus;
        setLinkState(data.state);
        setWebUrl(data.web_url);
        setRemoteChanged(data.remote_changed);
        setUnlinkDialogOpen(false);
        // pull-final overwrote the content server-side; reload the buffer so the
        // now-editable editor shows the final pulled version, not the stale one.
        if (mode === "pull-final") {
          try {
            const fresh = await fetch(`/api/documents/${encodeURIComponent(documentId)}`, {
              cache: "no-store",
            });
            if (fresh.ok) {
              adoptPulledDoc((await fresh.json()) as DocumentFull);
            }
          } catch {
            // Non-fatal: the next reload picks up the pulled content.
          }
        }
      } catch {
        setLinkError("Could not unlink from Google Docs.");
      } finally {
        setLinkBusy(false);
      }
    },
    [documentId, adoptPulledDoc],
  );

  // Toolbar active-states. useEditorState re-runs the selector on every editor
  // transaction and only re-renders the toolbar when a boolean actually flips —
  // cheaper than re-rendering on every keystroke.
  const marks = useEditorState({
    editor,
    selector: ({ editor: e }) =>
      e
        ? {
            bold: e.isActive("bold"),
            italic: e.isActive("italic"),
            h2: e.isActive("heading", { level: 2 }),
            h3: e.isActive("heading", { level: 3 }),
            bulletList: e.isActive("bulletList"),
            orderedList: e.isActive("orderedList"),
          }
        : null,
  });

  const onTitleChange = useCallback(
    (value: string): void => {
      setTitle(value);
      titleRef.current = value;
      scheduleAutosave();
    },
    [scheduleAutosave],
  );

  // Schedule-on-calendar (Phase 47). The reverse of the calendar-first link
  // flow: create a calendar event already linked to THIS sermon in one POST (the
  // create proxy forwards `document_id` since Phase 47; the API ownership-checks
  // it). The popover owns the date/title/series inputs and we own the fetch,
  // mirroring the QuickCreatePopover contract. Resolves null on success (show the
  // confirmation, close the popover) or a human-readable error string shown
  // inline in the popover.
  const onScheduleSubmit = useCallback(
    async (input: {
      event_date: string;
      title: string;
      series: string | null;
    }): Promise<string | null> => {
      try {
        const res = await fetch("/api/sermon-events", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            event_date: input.event_date,
            title: input.title,
            series: input.series,
            document_id: documentId,
          }),
          cache: "no-store",
        });
        if (!res.ok) {
          return await readErrorMessage(res, "Could not schedule the sermon.");
        }
        setScheduledDate(input.event_date);
        setScheduleOpen(false);
        return null;
      } catch {
        return "Network error. Please try again.";
      }
    },
    [documentId],
  );

  return (
    <section>
      <div className="mb-4 flex items-baseline justify-between gap-4">
        <input
          aria-label="Sermon title"
          value={title}
          onChange={(e) => onTitleChange(e.target.value)}
          placeholder="Untitled sermon"
          // The title is part of the read-only lock while linked — the native
          // Doc owns the manuscript, so the title input is disabled too.
          disabled={isLinked}
          readOnly={isLinked}
          className="min-w-0 flex-1 border-0 border-gray-200 border-b bg-transparent pb-1 font-semibold text-xl focus:border-black focus:outline-none disabled:text-gray-500"
        />
        <Link href="/sermons" className="shrink-0 text-blue-600 text-sm hover:underline">
          ← Sermons
        </Link>
      </div>

      {/* Editing-externally banner (Phase 45). While LINKED the editor is HARD
          read-only: the formatting toolbar/citation/docx affordances are gone and
          this banner sits above the read-only editor offering Open / Pull / Unlink.
          role="status" (a polite live region — NOT role="alert", which the App
          Router route announcer already owns). web_url is the only external string,
          opened with rel="noopener noreferrer". */}
      {isLinked ? (
        <output
          data-testid="editing-externally-banner"
          className="mb-3 block rounded-lg border border-blue-300 bg-blue-50 p-3"
        >
          <p className="font-medium text-blue-900 text-sm">Editing externally in Google Docs</p>
          <p className="mt-1 text-blue-800 text-sm">
            This sermon is open in Google Docs and is read-only here. Make your edits there, then
            pull the changes back.
            {remoteChanged ? " Changes available in Google — Pull to update." : null}
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            {safeUrl !== null ? (
              <a
                href={safeUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="rounded bg-blue-700 px-3 py-1.5 font-medium text-sm text-white hover:bg-blue-800"
              >
                Open in Google Docs
              </a>
            ) : null}
            <button
              type="button"
              disabled={linkBusy}
              onClick={() => void onPull()}
              className="rounded border border-blue-300 bg-white px-3 py-1.5 font-medium text-blue-800 text-sm hover:bg-blue-100 disabled:opacity-50"
            >
              Pull changes
            </button>
            <button
              type="button"
              disabled={linkBusy}
              onClick={() => setUnlinkDialogOpen(true)}
              className="rounded border border-blue-300 bg-white px-3 py-1.5 font-medium text-blue-800 text-sm hover:bg-blue-100 disabled:opacity-50"
            >
              Unlink
            </button>
          </div>
        </output>
      ) : null}

      {/* The unlink choice dialog — the settled mandatory pull-final-vs-keep-app
          choice. Inline (not a portal) so the component-test can assert it without
          a dialog harness; role="dialog" + aria-modal for assistive tech. */}
      {unlinkDialogOpen ? (
        <dialog
          open
          aria-label="Unlink from Google Docs"
          data-testid="unlink-dialog"
          className="relative z-10 mb-3 block w-full rounded-lg border border-gray-300 bg-white p-3"
        >
          <p className="font-medium text-gray-900 text-sm">Unlink from Google Docs</p>
          <p className="mt-1 text-gray-700 text-sm">
            Keep the latest Google Docs version in this sermon, or keep the version stored here?
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={linkBusy}
              onClick={() => void onUnlink("pull-final")}
              className="rounded bg-black px-3 py-1.5 font-medium text-sm text-white hover:bg-gray-800 disabled:opacity-50"
            >
              Pull final copy &amp; unlink
            </button>
            <button
              type="button"
              disabled={linkBusy}
              onClick={() => void onUnlink("keep-app")}
              className="rounded border border-gray-300 bg-white px-3 py-1.5 font-medium text-gray-700 text-sm hover:bg-gray-50 disabled:opacity-50"
            >
              Keep this version &amp; unlink
            </button>
            <button
              type="button"
              disabled={linkBusy}
              onClick={() => setUnlinkDialogOpen(false)}
              className="ml-auto text-gray-600 text-sm hover:underline disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </dialog>
      ) : null}

      {isLinked ? null : (
        <div className="mb-3 flex flex-wrap items-center gap-1">
          <ToolbarButton
            label="Bold"
            active={marks?.bold ?? false}
            disabled={!editor}
            onClick={() => editor?.chain().focus().toggleBold().run()}
          >
            <span className="font-bold">B</span>
          </ToolbarButton>
          <ToolbarButton
            label="Italic"
            active={marks?.italic ?? false}
            disabled={!editor}
            onClick={() => editor?.chain().focus().toggleItalic().run()}
          >
            <span className="italic">I</span>
          </ToolbarButton>
          <ToolbarButton
            label="Heading 2"
            active={marks?.h2 ?? false}
            disabled={!editor}
            onClick={() => editor?.chain().focus().toggleHeading({ level: 2 }).run()}
          >
            H2
          </ToolbarButton>
          <ToolbarButton
            label="Heading 3"
            active={marks?.h3 ?? false}
            disabled={!editor}
            onClick={() => editor?.chain().focus().toggleHeading({ level: 3 }).run()}
          >
            H3
          </ToolbarButton>
          <ToolbarButton
            label="Bullet list"
            active={marks?.bulletList ?? false}
            disabled={!editor}
            onClick={() => editor?.chain().focus().toggleBulletList().run()}
          >
            • List
          </ToolbarButton>
          <ToolbarButton
            label="Numbered list"
            active={marks?.orderedList ?? false}
            disabled={!editor}
            onClick={() => editor?.chain().focus().toggleOrderedList().run()}
          >
            1. List
          </ToolbarButton>

          <button
            type="button"
            aria-label="Cite from your library"
            aria-expanded={drawerOpen}
            disabled={!editor}
            onClick={() => setDrawerOpen((open) => !open)}
            className="rounded border border-gray-300 bg-white px-2 py-1 text-gray-700 text-sm disabled:opacity-50"
          >
            + Citation
          </button>

          {/* Scope (Phase 50). Opens a popover of the user's library books +
            collections; ticking limits the citation drawer's search to that set
            and persists per-sermon via the autosave path. The count badge shows
            how many books/collections are scoped (0 = whole library). */}
          <button
            type="button"
            aria-label="Scope citations to selected books"
            aria-expanded={scopeOpen}
            onClick={() => setScopeOpen((open) => !open)}
            className="rounded border border-gray-300 bg-white px-2 py-1 text-gray-700 text-sm disabled:opacity-50"
          >
            Scope{scopeCount > 0 ? ` (${scopeCount})` : ""}
          </button>

          {/* Schedule on calendar (Phase 47). Opens a popover that creates a
            calendar event linked to this sermon. Does NOT depend on the editor
            instance (it uses the title state + document_id), so it stays usable
            even before TipTap mounts. */}
          <button
            type="button"
            aria-label="Schedule on calendar"
            aria-expanded={scheduleOpen}
            onClick={() => setScheduleOpen((open) => !open)}
            className="rounded border border-gray-300 bg-white px-2 py-1 text-gray-700 text-sm disabled:opacity-50"
          >
            📅 Schedule
          </button>

          {/* DOCX round-trip (Phase 43). Download streams the export proxy as a
            blob and triggers a browser download; Import proxies its click to the
            hidden file input below, then POSTs the chosen .docx and reloads the
            editor with the returned TipTap JSON. Both disable while busy. */}
          <button
            type="button"
            aria-label="Download as Word document"
            disabled={!editor || docxBusy}
            onClick={() => void onExport()}
            className="rounded border border-gray-300 bg-white px-2 py-1 text-gray-700 text-sm disabled:opacity-50"
          >
            Download .docx
          </button>
          <button
            type="button"
            aria-label="Import a Word document"
            disabled={!editor || docxBusy}
            onClick={() => importInputRef.current?.click()}
            className="rounded border border-gray-300 bg-white px-2 py-1 text-gray-700 text-sm disabled:opacity-50"
          >
            Import .docx
          </button>
          <input
            ref={importInputRef}
            type="file"
            aria-label="Word document to import"
            accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            className="sr-only"
            onChange={(e) => {
              const file = e.target.files?.[0];
              // Reset first so re-choosing the same file re-fires onChange.
              e.target.value = "";
              if (file) {
                void onImportFile(file);
              }
            }}
          />

          {/* Link to Google Docs (Phase 45). Only when a Google connection exists;
            otherwise a hint pointing at Settings. Linking converts the sermon to a
            Doc, creates it in Drive, and flips the editor into read-only linked
            mode on success. */}
          {googleConnected ? (
            <button
              type="button"
              aria-label="Link to Google Docs"
              disabled={!editor || linkBusy}
              onClick={() => void onLink()}
              className="rounded border border-blue-300 bg-white px-2 py-1 text-blue-700 text-sm disabled:opacity-50"
            >
              Link to Google Docs
            </button>
          ) : (
            <Link
              href="/settings/integrations"
              data-testid="connect-google-hint"
              className="rounded border border-gray-300 bg-white px-2 py-1 text-gray-700 text-sm hover:bg-gray-50"
            >
              Connect Google in Settings
            </Link>
          )}

          <div className="ml-auto flex items-center gap-3">
            <SaveIndicator status={status} />
          </div>
        </div>
      )}

      {/* Link/pull/unlink failure (Phase 45) — the API's 409/400/502 detail in a
          visible, dismissable banner, distinct from the save + docx errors. */}
      {linkError !== null ? (
        <div
          role="alert"
          data-testid="link-error"
          className="mb-3 flex items-start justify-between gap-3 rounded-lg border border-red-300 bg-red-50 p-3"
        >
          <p className="text-red-700 text-sm">{linkError}</p>
          <button
            type="button"
            aria-label="Dismiss link error"
            onClick={() => setLinkError(null)}
            className="shrink-0 text-red-700 text-sm hover:underline"
          >
            Dismiss
          </button>
        </div>
      ) : null}

      {scopeOpen && !isLinked ? (
        <ScopePopover
          bookTitles={titleMap}
          collections={collections}
          scopeBookIds={scopeBookIds}
          scopeCollectionIds={scopeCollectionIds}
          onToggleBook={toggleScopeBook}
          onToggleCollection={toggleScopeCollection}
          onClose={() => setScopeOpen(false)}
        />
      ) : null}

      {drawerOpen && !isLinked ? (
        <LibraryDrawer
          editor={editor}
          bookTitles={titleMap}
          scope={drawerScope}
          onClose={() => setDrawerOpen(false)}
        />
      ) : null}

      {scheduleOpen && !isLinked ? (
        <ScheduleSermonPopover
          defaultDate={today()}
          defaultTitle={title}
          onClose={() => setScheduleOpen(false)}
          onSubmit={onScheduleSubmit}
        />
      ) : null}

      {/* Schedule confirmation (Phase 47). A polite status (NOT an alert) with a
          deep link to the scheduled day on the calendar; dismissable. */}
      {scheduledDate !== null ? (
        <output
          data-testid="schedule-confirmation"
          className="mt-3 flex items-start justify-between gap-3 rounded-lg border border-green-300 bg-green-50 p-3"
        >
          <p className="text-green-800 text-sm">
            Scheduled for {scheduledDate}.{" "}
            <Link
              href={`/calendar?view=month&date=${scheduledDate}`}
              className="font-medium underline hover:no-underline"
            >
              View on calendar
            </Link>
          </p>
          <button
            type="button"
            aria-label="Dismiss schedule confirmation"
            onClick={() => setScheduledDate(null)}
            className="shrink-0 text-green-800 text-sm hover:underline"
          >
            Dismiss
          </button>
        </output>
      ) : null}

      <LibraryMembershipProvider ownedBookIds={membership}>
        <EditorContent editor={editor} />
      </LibraryMembershipProvider>

      {status === "conflict" ? (
        <div role="alert" className="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-3">
          <p className="text-amber-800 text-sm">{CONFLICT_MESSAGE}</p>
          <button
            type="button"
            onClick={() => void onReloadLatest()}
            className="mt-2 rounded bg-amber-700 px-3 py-1.5 font-medium text-sm text-white hover:bg-amber-800"
          >
            Reload latest
          </button>
        </div>
      ) : null}

      {status === "error" ? (
        <p role="alert" className="mt-3 text-red-600 text-sm">
          Could not save the sermon. Your edits here are safe — autosave will retry as you type.
        </p>
      ) : null}

      {docxError !== null ? (
        <div
          role="alert"
          data-testid="docx-error"
          className="mt-3 flex items-start justify-between gap-3 rounded-lg border border-red-300 bg-red-50 p-3"
        >
          <p className="text-red-700 text-sm">{docxError}</p>
          <button
            type="button"
            aria-label="Dismiss error"
            onClick={() => setDocxError(null)}
            className="shrink-0 text-red-700 text-sm hover:underline"
          >
            Dismiss
          </button>
        </div>
      ) : null}
    </section>
  );
}

/** The save-state indicator: saved / saving / unsaved / error / conflict. */
function SaveIndicator({ status }: { status: SaveStatus }) {
  const text =
    status === "saving"
      ? "Saving…"
      : status === "saved"
        ? "Saved"
        : status === "unsaved"
          ? "Unsaved changes"
          : status === "conflict"
            ? "Conflict"
            : "Save failed";
  const tone =
    status === "saved"
      ? "text-gray-500"
      : status === "saving" || status === "unsaved"
        ? "text-gray-600"
        : "text-red-600";
  return (
    <span aria-live="polite" data-save-status={status} className={`text-sm ${tone}`}>
      {text}
    </span>
  );
}

function ToolbarButton({
  label,
  active,
  disabled,
  onClick,
  children,
}: {
  label: string;
  active: boolean;
  disabled: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={active}
      disabled={disabled}
      onClick={onClick}
      className={`rounded border px-2 py-1 text-sm disabled:opacity-50 ${
        active ? "border-black bg-black text-white" : "border-gray-300 bg-white text-gray-700"
      }`}
    >
      {children}
    </button>
  );
}

/**
 * The per-sermon Scope popover (Phase 50). A checkbox list of the user's library
 * books and collections; ticking limits the citation drawer's search to that
 * set, and the choice persists per-sermon through the autosave path. An EMPTY
 * selection means the whole library (the citation drawer omits the scope). Books
 * come from the shell's one-shot {book_id -> title} map; collections from the
 * server-fetched list. All labels render as PLAIN TEXT — never
 * dangerouslySetInnerHTML.
 */
function ScopePopover({
  bookTitles,
  collections,
  scopeBookIds,
  scopeCollectionIds,
  onToggleBook,
  onToggleCollection,
  onClose,
}: {
  bookTitles: ReadonlyMap<string, string>;
  collections: Collection[];
  scopeBookIds: string[];
  scopeCollectionIds: string[];
  onToggleBook: (bookId: string) => void;
  onToggleCollection: (collectionId: string) => void;
  onClose: () => void;
}) {
  const bookEntries = useMemo(() => [...bookTitles.entries()], [bookTitles]);
  const bookSet = useMemo(() => new Set(scopeBookIds), [scopeBookIds]);
  const collectionSet = useMemo(() => new Set(scopeCollectionIds), [scopeCollectionIds]);
  const empty = bookEntries.length === 0 && collections.length === 0;

  return (
    <aside
      aria-label="Scope citations to your library"
      data-testid="scope-popover"
      className="mb-3 rounded-lg border border-gray-300 bg-gray-50 p-4"
    >
      <div className="mb-2 flex items-baseline justify-between gap-4">
        <h2 className="font-semibold text-sm">Limit citations to…</h2>
        <button
          type="button"
          onClick={onClose}
          className="shrink-0 text-blue-600 text-xs hover:underline"
        >
          Close
        </button>
      </div>
      <p className="mb-3 text-gray-600 text-xs">
        Tick books or collections to scope the citation search for this sermon. Leave everything
        unticked to search your whole library.
      </p>

      {empty ? (
        <p className="rounded-lg border border-gray-300 border-dashed p-4 text-center text-gray-600 text-sm">
          Your library is empty.
        </p>
      ) : null}

      {collections.length > 0 ? (
        <div className="mb-3">
          <h3 className="mb-1 font-medium text-gray-700 text-xs uppercase tracking-wide">
            Collections
          </h3>
          <ul className="space-y-1">
            {collections.map((collection) => (
              <li key={collection.collection_id}>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={collectionSet.has(collection.collection_id)}
                    onChange={() => onToggleCollection(collection.collection_id)}
                  />
                  <span className="min-w-0 truncate">{collection.name}</span>
                  <span className="text-gray-500 text-xs">
                    {`${collection.book_ids.length} ${
                      collection.book_ids.length === 1 ? "book" : "books"
                    }`}
                  </span>
                </label>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {bookEntries.length > 0 ? (
        <div>
          <h3 className="mb-1 font-medium text-gray-700 text-xs uppercase tracking-wide">Books</h3>
          <ul className="max-h-56 space-y-1 overflow-y-auto">
            {bookEntries.map(([bookId, bookTitle]) => (
              <li key={bookId}>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={bookSet.has(bookId)}
                    onChange={() => onToggleBook(bookId)}
                  />
                  <span className="min-w-0 truncate">{bookTitle}</span>
                </label>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </aside>
  );
}
