import type { CollectionBooksRequest, CollectionCreate, CollectionPatch } from "./types";

/**
 * Pure structural whitelists for the collection mutation proxy routes
 * (app/api/collections/**, Phase 48). No DOM, no server-only imports —
 * unit-tested in test/collections.test.ts and safe to run on the edge.
 *
 * These mirror lib/calendar.ts:whitelistCreateEvent / whitelistPatchEvent — a
 * STRUCTURAL whitelist that re-serializes ONLY the allowed fields into a FRESH
 * object so nothing else (a smuggled `user_id`, `collection_id`, …) reaches the
 * API's `extra="forbid"` gate.
 *
 * Checks here are STRUCTURAL only (object-ness + primitive types). Every
 * length/range/cap/at-least-one-of rule stays with the API so the 422 contract
 * has exactly one owner (api/collections_routes.py): name 1..255, description
 * <= 2000, book_ids 1..10000, the "PATCH must set at least one field" rule, and
 * the library clamp on the add-books path (a foreign `book_id` is dropped
 * server-side, never an error this layer pre-derives).
 */

export type CollectionBodyResult<T> = { ok: true; body: T } | { ok: false; error: string };

/** Reject anything that is not a plain JSON object (arrays and null included). */
function asRecord(body: unknown): Record<string, unknown> | null {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return null;
  }
  return body as Record<string, unknown>;
}

/**
 * Whitelist for the POST /collections proxy body. Forwards `name` (required
 * string) plus `description` when present — a `string | null` (null is a
 * meaningful "no description" value the client may send). Every other key is
 * dropped by building a fresh object; an absent `description` is OMITTED (not
 * sent as null) so the API's default applies. Length validation (name 1..255,
 * description <= 2000) is left to the API (422).
 */
export function whitelistCreateCollection(body: unknown): CollectionBodyResult<CollectionCreate> {
  const record = asRecord(body);
  if (record === null) {
    return { ok: false, error: "Request body must be a JSON object." };
  }
  if (typeof record.name !== "string") {
    return { ok: false, error: "name must be a string." };
  }
  const out: CollectionCreate = { name: record.name };
  if (record.description !== undefined) {
    if (record.description !== null && typeof record.description !== "string") {
      return { ok: false, error: "description must be a string or null." };
    }
    out.description = record.description;
  }
  return { ok: true, body: out };
}

/**
 * Whitelist for the PATCH /collections/{id} proxy body. Forwards `name` and/or
 * `description` when present; every other key is dropped. There is NO required
 * token (collections have no optimistic-concurrency field). An absent optional
 * field is OMITTED from the forwarded body so the API's three-state PATCH
 * semantics see only what the client set; `description: null` is forwarded
 * VERBATIM to CLEAR the description (a present `null` must survive — a
 * truthiness guard would silently drop the clear). `name` is the NOT-NULL
 * column, so a present-and-null `name` is rejected here as a non-string (the
 * lib/calendar.ts title posture); the at-least-one-of rule and length checks
 * are the API's 422 to own.
 */
export function whitelistPatchCollection(body: unknown): CollectionBodyResult<CollectionPatch> {
  const record = asRecord(body);
  if (record === null) {
    return { ok: false, error: "Request body must be a JSON object." };
  }
  const out: CollectionPatch = {};
  if (record.name !== undefined) {
    if (typeof record.name !== "string") {
      return { ok: false, error: "name must be a string." };
    }
    out.name = record.name;
  }
  if (record.description !== undefined) {
    if (record.description !== null && typeof record.description !== "string") {
      return { ok: false, error: "description must be a string or null." };
    }
    out.description = record.description;
  }
  return { ok: true, body: out };
}

/**
 * Whitelist for the POST/DELETE /collections/{id}/books proxy body. Forwards
 * ONLY `book_ids` — a required array of strings, re-serialized into a fresh
 * array so a smuggled `user_id`/`collection_id` never rides along. Every
 * element must be a string (structural only); the cap (1..10000) and the
 * library-ownership clamp are the API's to own. An empty array passes
 * structurally — the API's `min_length=1` is the 422 owner.
 */
export function whitelistCollectionBooks(
  body: unknown,
): CollectionBodyResult<CollectionBooksRequest> {
  const record = asRecord(body);
  if (record === null) {
    return { ok: false, error: "Request body must be a JSON object." };
  }
  if (!Array.isArray(record.book_ids)) {
    return { ok: false, error: "book_ids must be an array of strings." };
  }
  const bookIds: string[] = [];
  for (const id of record.book_ids) {
    if (typeof id !== "string") {
      return { ok: false, error: "book_ids must be an array of strings." };
    }
    bookIds.push(id);
  }
  return { ok: true, body: { book_ids: bookIds } };
}
