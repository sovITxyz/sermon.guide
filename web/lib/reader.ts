import type { PositionUpdate } from "./types";

/**
 * Pure helpers for the reader proxy routes (app/api/books/**). No DOM, no
 * server-only imports — unit-tested in test/reader.test.ts.
 */

export type PositionBodyResult = { ok: true; body: PositionUpdate } | { ok: false; error: string };

/**
 * Structural whitelist for the PUT /books/{id}/position proxy body.
 *
 * Forwards ONLY `chunk_index` and `offset_ratio`; every other key is dropped
 * before the body is re-serialized upstream (the search-summary proxy
 * posture: the client can never widen or smuggle params through the proxy —
 * a `user_id` in the body never even reaches the API's `extra="forbid"`
 * gate). Checks are structural only: non-object bodies and wrong primitive
 * types are rejected here, while RANGE validation (`chunk_index >= 0`,
 * `offset_ratio` 0.0–1.0) is deliberately left to the API so the 422
 * contract has exactly one owner (api/reader.py PositionUpdate).
 *
 * `offset_ratio` is omitted from the forwarded body when absent so the API's
 * full-replace semantics clear the stored value to NULL.
 */
export function whitelistPositionUpdate(body: unknown): PositionBodyResult {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return { ok: false, error: "Request body must be a JSON object." };
  }
  const record = body as Record<string, unknown>;
  const chunkIndex = record.chunk_index;
  if (typeof chunkIndex !== "number") {
    return { ok: false, error: "chunk_index must be a number." };
  }
  const offsetRatio = record.offset_ratio;
  if (offsetRatio === undefined) {
    return { ok: true, body: { chunk_index: chunkIndex } };
  }
  if (offsetRatio !== null && typeof offsetRatio !== "number") {
    return { ok: false, error: "offset_ratio must be a number or null." };
  }
  return { ok: true, body: { chunk_index: chunkIndex, offset_ratio: offsetRatio } };
}
