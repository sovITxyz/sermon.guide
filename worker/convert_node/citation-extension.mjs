// Canonical React-free `citation` node for the Phase 43 DOCX round-trip.
//
// MIRROR-NOT-IMPORT CONTRACT (must stay in lockstep with the editor):
//   web/components/editor/CitationNode.tsx is the source of truth for the
//   citation node's *attrs* (bookId / chunkIndex / bookTitle / snippet /
//   parentSection). This file is the Node leg's mirror of that contract — the
//   same mirror-not-import discipline api/ uses for _ALLOWED_UPLOAD_MIMES.
//   web/ cannot be imported here (it is React + Next.js); this module re-states
//   the node so `@tiptap/html` keeps the citation when serializing/parsing.
//   If web's CitationNode attr set changes, change this file in the same PR.
//
// WHY A HYPERLINK, NOT data-* (the phase gate):
//   The editor's CitationNode serializes attrs as `data-*` on a
//   `div[data-type="citation"]`. Those `data-*` attributes DO NOT survive a
//   pandoc HTML->DOCX->HTML round-trip — only hyperlinks (`<a href>`) do. So on
//   the EXPORT leg this node's `renderHTML` emits a real anchor:
//
//       <a href="/read/{bookId}?chunk={chunkIndex}">{bookTitle || label}</a>
//
//   matching the `readHref(bookId, chunkIndex)` shape from web/lib/library.ts.
//   On the IMPORT leg `parseHTML` matches `a[href^="/read/"]` and RECOVERS
//   bookId + chunkIndex from the URL, rebuilding the citation node.
//
//   bookTitle / snippet / parentSection are DEGRADED on import (best-effort):
//   the title is taken from the anchor text when present, snippet is lost
//   (the docx never carried it), parentSection is lost. That is the accepted
//   fidelity ceiling — the deep-link (bookId + chunkIndex) is the load-bearing
//   datum and it survives losslessly.
//
// SECURITY: the import side is attacker-controlled (a user-uploaded docx). The
//   href parser ONLY rebuilds a citation from a same-origin `/read/<id>` path;
//   absolute URLs, `javascript:`, `data:`, protocol-relative `//host`, and any
//   other scheme are NOT matched here and therefore never become a citation
//   node. StarterKit's Link is disabled, so such links degrade to plain text
//   downstream (zero clickable attacker links land in the stored JSON).

import { Node, mergeAttributes } from "@tiptap/core";

// The reader deep-link path prefix. Mirrors `readHref` in web/lib/library.ts:
//   readHref(bookId, chunkIndex) === `/read/${encodeURIComponent(bookId)}?chunk=${chunkIndex}`
const READ_PREFIX = "/read/";

/** Coerce a serialized chunk index back to a finite, non-negative integer. */
function parseChunkIndex(value) {
  const n = typeof value === "string" ? Number.parseInt(value, 10) : Number(value);
  return Number.isFinite(n) && n >= 0 ? n : 0;
}

/**
 * Build the reader href for a citation. Identical shape to web's `readHref`
 * (encodeURIComponent on the id, `?chunk=` query). chunkIndex is always present
 * on the export side, so the query is always emitted.
 */
function readHref(bookId, chunkIndex) {
  return `${READ_PREFIX}${encodeURIComponent(String(bookId ?? ""))}?chunk=${chunkIndex}`;
}

/**
 * Recover { bookId, chunkIndex } from a `/read/<id>?chunk=<n>` href, or null if
 * the href is not a same-origin reader deep-link. Parsing against a dummy base
 * makes the URL parser reject absolute/scheme-bearing hrefs: an attacker href
 * like `https://evil/...` or `javascript:...` produces a `url.pathname`/origin
 * that does not start with our prefix, so it returns null and never becomes a
 * citation.
 */
function parseReadHref(href) {
  if (typeof href !== "string" || !href.startsWith(READ_PREFIX)) {
    return null;
  }
  let url;
  try {
    // Dummy origin: a genuine same-origin `/read/...` keeps that origin; an
    // absolute or scheme-bearing href adopts its own origin and fails the
    // pathname-prefix check below.
    url = new URL(href, "http://sermon.invalid");
  } catch {
    return null;
  }
  if (url.origin !== "http://sermon.invalid" || !url.pathname.startsWith(READ_PREFIX)) {
    return null;
  }
  const rawId = url.pathname.slice(READ_PREFIX.length);
  if (!rawId) {
    return null;
  }
  let bookId;
  try {
    bookId = decodeURIComponent(rawId);
  } catch {
    bookId = rawId;
  }
  // A `/read/<id>` path segment must not contain a nested slash (a citation id
  // is a single opaque segment); reject deeper paths.
  if (bookId.includes("/")) {
    return null;
  }
  return { bookId, chunkIndex: parseChunkIndex(url.searchParams.get("chunk")) };
}

/**
 * The canonical citation node. Same `name`/`group`/`atom` shape as web's
 * CitationNode so `generateJSON` emits a node the editor recognizes. No node
 * view (this leg never renders to a screen).
 */
export const CitationNode = Node.create({
  name: "citation",
  group: "block",
  atom: true,
  selectable: true,
  draggable: false,

  addAttributes() {
    return {
      bookId: { default: "" },
      chunkIndex: { default: 0 },
      bookTitle: { default: "" },
      // snippet / parentSection are part of the editor's attr contract; they
      // cannot survive docx (no carrier) but are declared so a JSON->HTML->JSON
      // pass within this leg keeps them and the node shape matches the editor's.
      snippet: { default: "" },
      parentSection: { default: null },
    };
  },

  parseHTML() {
    return [
      {
        tag: `a[href^="${READ_PREFIX}"]`,
        getAttrs: (el) => {
          const parsed = parseReadHref(el.getAttribute("href"));
          if (parsed === null) {
            // Not a reader deep-link — let other extensions / plain text handle
            // it; do not build a citation.
            return false;
          }
          // bookTitle is degraded-from-text on import (the docx dropped the
          // data-* title); fall back to the anchor's own text content, with
          // internal whitespace collapsed — pandoc can inject soft line breaks
          // inside the anchor when wrapping the .docx hyperlink.
          const text = (el.textContent ?? "").replace(/\s+/g, " ").trim();
          return {
            bookId: parsed.bookId,
            chunkIndex: parsed.chunkIndex,
            bookTitle: text,
            snippet: "",
            parentSection: null,
          };
        },
      },
    ];
  },

  renderHTML({ node }) {
    const { bookId, chunkIndex, bookTitle } = node.attrs;
    const label = String(bookTitle ?? "").trim() || `Citation: ${bookId}`;
    return [
      "a",
      mergeAttributes({
        href: readHref(bookId, parseChunkIndex(chunkIndex)),
        "data-type": "citation",
      }),
      label,
    ];
  },
});

// Exposed for the round-trip test / debugging.
export { parseReadHref, readHref };
