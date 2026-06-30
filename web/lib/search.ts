import type { SearchRequest } from "./types";

/**
 * Pure helper for the search proxy route (app/api/search/route.ts). No DOM, no
 * server-only imports — unit-tested in test/search.test.ts and safe on the edge.
 *
 * Mirrors lib/documents.ts:whitelistCreateDocument — a STRUCTURAL whitelist
 * that re-serializes ONLY the allowed fields upstream. A client can never smuggle
 * `limit`/`rerank` (which would widen the retrieval fan-out or flip off the
 * rerank/highlight pipeline) or a `user_id` (which the API's `extra="forbid"`
 * would 422 on) through the proxy: every key other than `query` and the Phase 49
 * scope fields is dropped before the body reaches the API. The checks are
 * structural only (object-ness, `query` is a string, the scope fields are
 * arrays of strings) — the min 1 / max 1024 query length and the per-array
 * caps stay with the API (api/search.py SearchRequest) so the 422 has one owner.
 *
 * SCOPE (Phase 49): `book_ids`/`collection_ids` are FORWARDED when present so the
 * API can intersect them with the JWT user's library (an INTERSECTION — they can
 * only shrink the search, never widen it); OMITTED when absent (= whole library).
 */

export type SearchBodyResult = { ok: true; body: SearchRequest } | { ok: false; error: string };

/** Reject anything that is not a plain JSON object (arrays and null included). */
function asRecord(body: unknown): Record<string, unknown> | null {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return null;
  }
  return body as Record<string, unknown>;
}

/**
 * Structural check for an optional `string[]` scope field. Returns the fresh
 * array when `value` is an array of strings, or `null` to signal "not an array
 * of strings" (the caller turns that into a 400). An empty array passes
 * structurally — the API owns the min/max caps.
 */
export function asStringArray(value: unknown): string[] | null {
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
 * Whitelist for the POST /search proxy body. Forwards `query` (string) plus the
 * optional `book_ids`/`collection_ids` scope arrays when present, by constructing
 * a fresh object; every other key — including `limit`, `rerank`, and a smuggled
 * `user_id` — is dropped. A scope field is OMITTED when absent (null counts as
 * absent: the API's `| None` default = whole library) and rejected with a 400
 * only when present-but-not-an-array-of-strings. Query length and the per-array
 * caps are left to the API (422).
 */
export function whitelistSearch(body: unknown): SearchBodyResult {
  const record = asRecord(body);
  if (record === null) {
    return { ok: false, error: "Request body must be a JSON object." };
  }
  if (typeof record.query !== "string") {
    return { ok: false, error: "query must be a string." };
  }
  const out: SearchRequest = { query: record.query };
  if (record.book_ids !== undefined && record.book_ids !== null) {
    const bookIds = asStringArray(record.book_ids);
    if (bookIds === null) {
      return { ok: false, error: "book_ids must be an array of strings." };
    }
    out.book_ids = bookIds;
  }
  if (record.collection_ids !== undefined && record.collection_ids !== null) {
    const collectionIds = asStringArray(record.collection_ids);
    if (collectionIds === null) {
      return { ok: false, error: "collection_ids must be an array of strings." };
    }
    out.collection_ids = collectionIds;
  }
  return { ok: true, body: out };
}
