"use client";

import type { DocumentFull, ProseMirrorDoc } from "@/lib/types";
import Placeholder from "@tiptap/extension-placeholder";
import { EditorContent, useEditor, useEditorState } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Link from "next/link";
import { useCallback, useRef, useState } from "react";

/**
 * Manuscript editor (Phase 35, B2 slice B). A headless TipTap contenteditable
 * with a fixed toolbar, an editable title, and an EXPLICIT Save button — NO
 * autosave (that is Phase 36), NO citations (Phase 37).
 *
 * Bundling: this module is dynamic-imported with `ssr: false` from the route
 * shell (SermonEditorShell), so TipTap loads only on the editor route. `useEditor`
 * runs `immediatelyRender: false` per the App Router SSR requirement.
 *
 * Save: PATCH {title, content: editor.getJSON(), base_updated_at} through the
 * same-origin /api/documents/[id] proxy. The proxy whitelists exactly those
 * three fields (lib/documents.ts). On 200 we adopt the returned `updated_at` as
 * the new in-memory `base_updated_at` (so the next save isn't a self-conflict).
 * On 409 (a write landed elsewhere since this tab loaded) we surface a
 * non-destructive inline error and KEEP the user's buffer — the full conflict
 * UX (reload/merge) is Phase 36. 404/413 get their own sensible messages.
 *
 * Security: TipTap is headless contenteditable — ZERO dangerouslySetInnerHTML.
 * The editor renders its own DOM from the ProseMirror document; we never inject
 * raw HTML, and the content round-trips as JSON, not markup.
 */

type SaveStatus =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "saved" }
  | { kind: "error"; message: string };

const CONFLICT_MESSAGE =
  "This sermon was changed in another tab or device since you opened it. " +
  "Your edits here are safe — copy anything you need, then reload to get the latest version.";

/** Build the StarterKit + Placeholder extension set. Link is disabled this
 * phase (no link UI in the toolbar; interactive links arrive with citations in
 * Phase 37). */
function buildExtensions() {
  return [
    StarterKit.configure({ link: false }),
    Placeholder.configure({ placeholder: "Start writing your sermon…" }),
  ];
}

export function SermonEditor({ document }: { document: DocumentFull }) {
  const [title, setTitle] = useState(document.title);
  const [status, setStatus] = useState<SaveStatus>({ kind: "idle" });
  // The optimistic-concurrency token. Starts at the loaded `updated_at` and is
  // advanced to the server's returned `updated_at` after every successful save,
  // so a second save from the same tab is never a false 409.
  const baseUpdatedAt = useRef(document.updated_at);

  const editor = useEditor({
    immediatelyRender: false,
    extensions: buildExtensions(),
    content: document.content,
    editorProps: {
      attributes: {
        class:
          "prose prose-sm max-w-none min-h-[24rem] rounded-lg border border-gray-300 p-4 focus:outline-none focus:ring-2 focus:ring-black/20",
      },
    },
  });

  // Toolbar active-states. useEditorState re-runs the selector on every editor
  // transaction (selection/content change) and only re-renders the toolbar when
  // a boolean actually flips — cheaper than re-rendering on every keystroke.
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

  const onSave = useCallback(async (): Promise<void> => {
    if (!editor) {
      return;
    }
    setStatus({ kind: "saving" });
    const content = editor.getJSON() as ProseMirrorDoc;
    try {
      const res = await fetch(`/api/documents/${encodeURIComponent(document.document_id)}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          title,
          content,
          base_updated_at: baseUpdatedAt.current,
        }),
      });

      if (res.status === 409) {
        // Non-destructive: keep the user's buffer untouched. Phase 36 owns the
        // reload/merge flow.
        setStatus({ kind: "error", message: CONFLICT_MESSAGE });
        return;
      }
      if (res.status === 413) {
        setStatus({
          kind: "error",
          message: "This sermon is too large to save. Trim some content and try again.",
        });
        return;
      }
      if (res.status === 404) {
        setStatus({
          kind: "error",
          message: "This sermon no longer exists. Your edits here are not saved.",
        });
        return;
      }
      if (!res.ok) {
        const data = (await res.json().catch(() => null)) as { error?: string } | null;
        setStatus({
          kind: "error",
          message: data?.error ?? "Could not save the sermon. Please try again.",
        });
        return;
      }

      const saved = (await res.json()) as DocumentFull;
      // Adopt the server's new updated_at so the next save isn't a self-conflict.
      baseUpdatedAt.current = saved.updated_at;
      setStatus({ kind: "saved" });
    } catch {
      setStatus({ kind: "error", message: "Network error. Please try again." });
    }
  }, [editor, document.document_id, title]);

  const saving = status.kind === "saving";

  return (
    <section>
      <div className="mb-4 flex items-baseline justify-between gap-4">
        <input
          aria-label="Sermon title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
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

        <div className="ml-auto flex items-center gap-3">
          {status.kind === "saved" ? <span className="text-gray-500 text-sm">Saved</span> : null}
          <button
            type="button"
            onClick={() => void onSave()}
            disabled={saving || !editor}
            className="rounded bg-black px-3 py-2 font-medium text-sm text-white disabled:opacity-50"
          >
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>

      <EditorContent editor={editor} />

      {status.kind === "error" ? (
        <p role="alert" className="mt-3 text-red-600 text-sm">
          {status.message}
        </p>
      ) : null}
    </section>
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
