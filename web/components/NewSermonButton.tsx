"use client";

import type { DocumentFull, ProseMirrorDoc } from "@/lib/types";
import { useRouter } from "next/navigation";
import { useState } from "react";

/**
 * Seed content for a brand-new sermon: a minimal-but-valid ProseMirror/TipTap
 * document (one empty paragraph inside a `doc` node). TipTap's StarterKit
 * renders this as an empty editor with the Placeholder showing; the API stores
 * it as opaque JSONB and derives an empty `content_text`. The internal shape is
 * the editor's contract — kept deliberately minimal so the create flow does not
 * pin a richer schema than the editor will round-trip.
 */
const SEED_CONTENT: ProseMirrorDoc = {
  type: "doc",
  content: [{ type: "paragraph" }],
};

const DEFAULT_TITLE = "Untitled sermon";

/**
 * The "new sermon" create flow. POSTs an empty seed document through the
 * same-origin proxy (/api/documents) — never to the API origin — then routes to
 * the editor at /sermons/[newId]. The proxy forwards only {title, content}
 * (lib/documents.ts whitelist); the 201 carries the full doc incl. its
 * `document_id`.
 */
export function NewSermonButton() {
  const router = useRouter();
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onCreate(): Promise<void> {
    if (creating) {
      return;
    }
    setError(null);
    setCreating(true);
    try {
      const res = await fetch("/api/documents", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ title: DEFAULT_TITLE, content: SEED_CONTENT }),
      });
      if (!res.ok) {
        const data = (await res.json().catch(() => null)) as { error?: string } | null;
        setError(data?.error ?? "Could not create the sermon. Please try again.");
        setCreating(false);
        return;
      }
      const doc = (await res.json()) as DocumentFull;
      router.push(`/sermons/${encodeURIComponent(doc.document_id)}`);
    } catch {
      setError("Network error. Please try again.");
      setCreating(false);
    }
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        type="button"
        onClick={onCreate}
        disabled={creating}
        className="rounded bg-black px-3 py-2 font-medium text-sm text-white disabled:opacity-50"
      >
        {creating ? "Creating…" : "New sermon"}
      </button>
      {error ? (
        <p role="alert" className="text-red-600 text-sm">
          {error}
        </p>
      ) : null}
    </div>
  );
}
