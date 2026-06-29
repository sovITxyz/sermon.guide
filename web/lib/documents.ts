import type { DocumentCreate, DocumentPatch, ProseMirrorDoc } from "./types";

/**
 * Pure helpers for the documents proxy routes (app/api/documents/**). No DOM,
 * no server-only imports — unit-tested in test/documents.test.ts and safe to
 * run on the edge.
 *
 * These mirror lib/reader.ts:whitelistPositionUpdate — a STRUCTURAL whitelist
 * that re-serializes ONLY the allowed fields upstream. A client can never
 * smuggle `user_id`/`content_text`/`schema_version`/`document_id`/`deleted_at`
 * through the proxy: they are dropped before the body ever reaches the API's
 * `extra="forbid"` gate. Checks are structural only (object-ness, primitive
 * types, content is a non-null JSON object) — title length, the
 * at-least-one-of rule, and the content byte cap stay with the API so the 422
 * contract has exactly one owner (api/documents.py).
 */

export type DocumentBodyResult<T> = { ok: true; body: T } | { ok: false; error: string };

/** Reject anything that is not a plain JSON object (arrays and null included). */
function asRecord(body: unknown): Record<string, unknown> | null {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return null;
  }
  return body as Record<string, unknown>;
}

/**
 * Structural check that `content` is a ProseMirror/TipTap JSON object — a
 * non-null, non-array object. Its INTERNAL shape (node types, marks) is the
 * editor's contract and is NOT validated here; the API stores it as opaque
 * JSONB and caps its serialized byte size (>2MB -> 413).
 */
function isProseMirrorDoc(value: unknown): value is ProseMirrorDoc {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Structural check for an optional `string[]` scope field (Phase 50). Returns a
 * fresh array when `value` is an array of strings, or `null` to signal "not an
 * array of strings" (the caller turns that into a 400). An empty array passes
 * structurally — the per-array caps (book_ids <= 10000, collection_ids <= 500)
 * and the ownership clamp stay with the API. Mirrors lib/search.ts:asStringArray
 * (kept local so this proxy module stays independent of the search proxy).
 */
function asStringArray(value: unknown): string[] | null {
  if (!Array.isArray(value)) {
    return null;
  }
  const out: string[] = [];
  for (const element of value) {
    if (typeof element !== "string") {
      return null;
    }
    out.push(element);
  }
  return out;
}

/**
 * Whitelist for the POST /documents proxy body. Forwards `title` (string) and
 * `content` (JSON object), plus the optional citation-scope arrays
 * `scope_book_ids`/`scope_collection_ids` (Phase 50) when present; every other
 * key is dropped by constructing a fresh object. A scope field is OMITTED when
 * absent (null counts as absent: the API defaults to whole library) and a 400
 * only when present-but-not-an-array-of-strings. Title length and the per-array
 * caps are left to the API (422).
 */
export function whitelistCreateDocument(body: unknown): DocumentBodyResult<DocumentCreate> {
  const record = asRecord(body);
  if (record === null) {
    return { ok: false, error: "Request body must be a JSON object." };
  }
  if (typeof record.title !== "string") {
    return { ok: false, error: "title must be a string." };
  }
  if (!isProseMirrorDoc(record.content)) {
    return { ok: false, error: "content must be a JSON object." };
  }
  const out: DocumentCreate = { title: record.title, content: record.content };
  if (record.scope_book_ids !== undefined && record.scope_book_ids !== null) {
    const bookIds = asStringArray(record.scope_book_ids);
    if (bookIds === null) {
      return { ok: false, error: "scope_book_ids must be an array of strings." };
    }
    out.scope_book_ids = bookIds;
  }
  if (record.scope_collection_ids !== undefined && record.scope_collection_ids !== null) {
    const collectionIds = asStringArray(record.scope_collection_ids);
    if (collectionIds === null) {
      return { ok: false, error: "scope_collection_ids must be an array of strings." };
    }
    out.scope_collection_ids = collectionIds;
  }
  return { ok: true, body: out };
}

/**
 * Whitelist for the PATCH /documents/{id} proxy body. Forwards ONLY
 * `base_updated_at` (REQUIRED string), plus `title` (string) and/or `content`
 * (JSON object) when present; every other key is dropped. An absent optional
 * field is omitted from the forwarded body entirely (not sent as null) so the
 * API's partial-update semantics see only what the client set. The
 * at-least-one-of-title/content rule is the API's 422 to own — this layer only
 * guarantees the structural shape and the required concurrency token.
 */
export function whitelistPatchDocument(body: unknown): DocumentBodyResult<DocumentPatch> {
  const record = asRecord(body);
  if (record === null) {
    return { ok: false, error: "Request body must be a JSON object." };
  }
  if (typeof record.base_updated_at !== "string") {
    return { ok: false, error: "base_updated_at must be a string." };
  }
  const out: DocumentPatch = { base_updated_at: record.base_updated_at };
  if (record.title !== undefined) {
    if (typeof record.title !== "string") {
      return { ok: false, error: "title must be a string." };
    }
    out.title = record.title;
  }
  if (record.content !== undefined) {
    if (!isProseMirrorDoc(record.content)) {
      return { ok: false, error: "content must be a JSON object." };
    }
    out.content = record.content;
  }
  // Citation-scope arrays (Phase 50). Present (incl. `[]`) forwards a fresh
  // array (the API replaces + clamps the stored scope); a null/absent field is
  // omitted so the API leaves the stored scope untouched.
  if (record.scope_book_ids !== undefined && record.scope_book_ids !== null) {
    const bookIds = asStringArray(record.scope_book_ids);
    if (bookIds === null) {
      return { ok: false, error: "scope_book_ids must be an array of strings." };
    }
    out.scope_book_ids = bookIds;
  }
  if (record.scope_collection_ids !== undefined && record.scope_collection_ids !== null) {
    const collectionIds = asStringArray(record.scope_collection_ids);
    if (collectionIds === null) {
      return { ok: false, error: "scope_collection_ids must be an array of strings." };
    }
    out.scope_collection_ids = collectionIds;
  }
  return { ok: true, body: out };
}
