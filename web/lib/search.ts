import type { SearchRequest } from "./types";

/**
 * Pure helper for the search proxy route (app/api/search/route.ts). No DOM, no
 * server-only imports — unit-tested in test/search.test.ts and safe on the edge.
 *
 * Mirrors lib/documents.ts:whitelistCreateDocument — a STRUCTURAL whitelist
 * that re-serializes ONLY the allowed field upstream. A client can never smuggle
 * `limit`/`rerank` (which would widen the retrieval fan-out or flip off the
 * rerank/highlight pipeline) or a `user_id`/`book_ids` (which the API's
 * `extra="forbid"` would 422 on) through the proxy: every key other than `query`
 * is dropped before the body reaches the API. The check is structural only
 * (object-ness, `query` is a string) — the min 1 / max 1024 length contract
 * stays with the API (api/search.py SearchRequest) so the 422 has one owner.
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
 * Whitelist for the POST /search proxy body. Forwards ONLY `query` (string) by
 * constructing a fresh object with just that field; every other key — including
 * `limit` and `rerank` — is dropped. Query length validation is left to the API
 * (min 1 / max 1024 -> 422).
 */
export function whitelistSearch(body: unknown): SearchBodyResult {
  const record = asRecord(body);
  if (record === null) {
    return { ok: false, error: "Request body must be a JSON object." };
  }
  if (typeof record.query !== "string") {
    return { ok: false, error: "query must be a string." };
  }
  return { ok: true, body: { query: record.query } };
}
