"use client";

import { readHref } from "@/lib/library";
import { displaySection } from "@/lib/summary";
import {
  Node,
  type NodeViewProps,
  NodeViewWrapper,
  ReactNodeViewRenderer,
  mergeAttributes,
} from "@tiptap/react";
import Link from "next/link";
import { useLibraryMembership } from "./library-membership";

/**
 * Citation node (Phase 37, B2 slice D — the signature B2 integration).
 *
 * A block-level ATOM: a cited library passage becomes a first-class manuscript
 * block, styled like the /search citation card and deep-linking into the reader
 * at the exact chunk. It is an atom (`atom: true`, no editable children) so the
 * user cannot type inside it — the snippet is CACHED at insert and the node is
 * self-contained.
 *
 * SELF-CONTAINED BY DESIGN: `bookTitle` and `snippet` are cached into the node's
 * attrs at insert time (from the search hit), so the node view renders PURELY
 * from attrs and NEVER refetches on render. That keeps the saved document
 * meaningful even if the cited book later leaves the user's library (the
 * degraded state below) or the book text changes.
 *
 * ROUND-TRIP CONTRACT (the load-bearing requirement): the attrs must survive
 * `editor.getJSON()` -> persist -> `setContent()` -> re-render. TipTap rebuilds
 * the attrs from JSON automatically via `addAttributes`; the `renderHTML` /
 * `parseHTML` `data-*` mapping additionally lets the node survive an HTML
 * clipboard round-trip and lets an existing doc with a citation parse on load.
 * The node is registered in the editor's extensions list (SermonEditor) so
 * stored docs containing it parse correctly when the editor opens.
 *
 * SECURITY: the snippet renders as PLAIN TEXT inside the React node view (JSX
 * children) — ZERO `dangerouslySetInnerHTML` (repo invariant). The card never
 * injects raw HTML; `displaySection` additionally drops `<`-bearing EPUB
 * tag-soup section labels, mirroring the /search card.
 */

export interface CitationAttrs {
  bookId: string;
  chunkIndex: number;
  bookTitle: string;
  snippet: string;
  parentSection: string | null;
}

const CITATION_TAG = "div";
const CITATION_DATA_TYPE = "citation";

/** Coerce a serialized `data-chunk-index` back to a finite integer (0 fallback). */
function parseChunkIndex(value: unknown): number {
  const n = typeof value === "string" ? Number.parseInt(value, 10) : Number(value);
  return Number.isFinite(n) ? n : 0;
}

export const CitationNode = Node.create({
  name: "citation",
  group: "block",
  atom: true,
  selectable: true,
  // A citation card is a self-contained block the user places, not drags around
  // mid-paragraph; keep it non-draggable per the pre-made decision.
  draggable: false,

  addAttributes() {
    return {
      bookId: {
        default: "",
        parseHTML: (el) => el.getAttribute("data-book-id") ?? "",
        renderHTML: (attrs) => ({ "data-book-id": String(attrs.bookId ?? "") }),
      },
      chunkIndex: {
        default: 0,
        parseHTML: (el) => parseChunkIndex(el.getAttribute("data-chunk-index")),
        renderHTML: (attrs) => ({ "data-chunk-index": String(attrs.chunkIndex ?? 0) }),
      },
      bookTitle: {
        default: "",
        parseHTML: (el) => el.getAttribute("data-book-title") ?? "",
        renderHTML: (attrs) => ({ "data-book-title": String(attrs.bookTitle ?? "") }),
      },
      snippet: {
        default: "",
        parseHTML: (el) => el.getAttribute("data-snippet") ?? "",
        renderHTML: (attrs) => ({ "data-snippet": String(attrs.snippet ?? "") }),
      },
      parentSection: {
        default: null,
        parseHTML: (el) => el.getAttribute("data-parent-section"),
        renderHTML: (attrs) =>
          attrs.parentSection == null ? {} : { "data-parent-section": String(attrs.parentSection) },
      },
    };
  },

  parseHTML() {
    return [{ tag: `${CITATION_TAG}[data-type="${CITATION_DATA_TYPE}"]` }];
  },

  renderHTML({ HTMLAttributes }) {
    return [CITATION_TAG, mergeAttributes({ "data-type": CITATION_DATA_TYPE }, HTMLAttributes)];
  },

  addNodeView() {
    return ReactNodeViewRenderer(CitationView);
  },
});

/**
 * The React node view: a card mirroring the /search citation-card styling. Reads
 * everything from `node.attrs` (cached at insert) — NEVER fetches. The
 * degraded badge is decided from the shared owned-`book_id` set in context (one
 * `/library` fetch for the whole doc, NOT per citation).
 */
export function CitationView({ node }: NodeViewProps) {
  const attrs = node.attrs as CitationAttrs;
  const ownedBookIds = useLibraryMembership();
  const owned = ownedBookIds.has(attrs.bookId);
  const section = displaySection(attrs.parentSection);
  const href = readHref(attrs.bookId, attrs.chunkIndex);

  return (
    <NodeViewWrapper
      data-type={CITATION_DATA_TYPE}
      className="my-3 rounded-lg border border-gray-200 p-4"
      contentEditable={false}
    >
      <div className="mb-2 flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="font-medium text-blue-700 text-xs">Citation</span>
        <span className="font-medium text-sm">{attrs.bookTitle}</span>
        <span className="text-gray-500 text-xs">
          {section ? `${section} · ` : ""}
          chunk {attrs.chunkIndex}
        </span>
        {owned ? (
          <Link
            href={href}
            rel="noopener"
            className="text-blue-600 text-xs hover:underline"
            data-testid="citation-read-link"
          >
            Read in context
          </Link>
        ) : (
          <span
            className="rounded bg-amber-100 px-1.5 py-0.5 font-medium text-amber-800 text-xs"
            data-testid="citation-degraded-badge"
          >
            No longer in your library
          </span>
        )}
      </div>
      <p className="line-clamp-4 whitespace-pre-wrap text-gray-600 text-sm">{attrs.snippet}</p>
    </NodeViewWrapper>
  );
}
