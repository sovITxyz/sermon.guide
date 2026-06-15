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
 * Whitelist for the POST /documents proxy body. Forwards ONLY `title` (string)
 * and `content` (JSON object); every other key is dropped by constructing a
 * fresh object with just those two fields. Title length validation is left to
 * the API (min 1 / max 512 -> 422).
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
  return { ok: true, body: { title: record.title, content: record.content } };
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
  return { ok: true, body: out };
}
