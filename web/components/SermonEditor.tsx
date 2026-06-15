"use client";

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
import type { DocumentFull, ProseMirrorDoc } from "@/lib/types";
import Placeholder from "@tiptap/extension-placeholder";
import { EditorContent, useEditor, useEditorState } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
}: {
  document: DocumentFull;
  // The user's owned-`book_id` set, resolved ONCE by the shell's single
  // /library fetch on doc open and shared with every citation node view via
  // context — so the degraded badge costs ZERO per-citation fetches. Defaults to
  // an empty set (everything degraded) when the shell does not supply one.
  ownedBookIds?: ReadonlySet<string>;
  // The {book_id -> title} map from the SAME one-shot /library fetch — the only
  // source of a citation's title (a raw /search hit carries none). Used by the
  // in-editor LibraryDrawer to cache `bookTitle` at insert. Empty by default.
  bookTitles?: ReadonlyMap<string, string>;
}) {
  const [title, setTitle] = useState(initialDocument.title);
  const [status, setStatus] = useState<SaveStatus>("saved");
  // The LibraryDrawer is opened from a toolbar affordance; closed by default so
  // the editor opens uncluttered.
  const [drawerOpen, setDrawerOpen] = useState(false);

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
  });
  // Latest title kept in a ref so the autosave loop (and the pagehide flush, a
  // non-React event) reads the current value, not a stale render closure.
  const titleRef = useRef(initialDocument.title);
  const flight = useRef<FlightState>(idleFlight());
  // Once a 409 lands, the loop STOPS until the user reloads. Guards every
  // scheduler entry point (debounce, max-interval, trailing) so a conflicted
  // tab never silently re-PATCHes over the other side.
  const conflicted = useRef(false);
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
    return { title: titleRef.current, content: editor.getJSON() as ProseMirrorDoc };
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
    if (conflicted.current) {
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
    if (conflicted.current) {
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

  // pagehide flush via fetch keepalive: only when dirty AND within the ~64 KB
  // keepalive ceiling. Oversize -> SKIP silently (next-open save covers it).
  // Also flushes on unmount (SPA navigations never fire pagehide).
  useEffect(() => {
    mounted.current = true;
    const flush = (): void => {
      if (conflicted.current) {
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
      lastSaved.current = { title: latest.title, content: latest.content };
      flight.current = idleFlight();
      conflicted.current = false;
      setStatus("saved");
    } catch {
      setStatus("conflict");
    }
  }, [editor, documentId]);

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

  return (
    <section>
      <div className="mb-4 flex items-baseline justify-between gap-4">
        <input
          aria-label="Sermon title"
          value={title}
          onChange={(e) => onTitleChange(e.target.value)}
          placeholder="Untitled sermon"
          className="min-w-0 flex-1 border-0 border-gray-200 border-b bg-transparent pb-1 font-semibold text-xl focus:border-black focus:outline-none"
        />
        <Link href="/sermons" className="shrink-0 text-blue-600 text-sm hover:underline">
          ← Sermons
        </Link>
      </div>

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

        <div className="ml-auto flex items-center gap-3">
          <SaveIndicator status={status} />
        </div>
      </div>

      {drawerOpen ? (
        <LibraryDrawer editor={editor} bookTitles={titleMap} onClose={() => setDrawerOpen(false)} />
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
