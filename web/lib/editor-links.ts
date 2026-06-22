import type { UnlinkMode, UnlinkRequest } from "./types";

/**
 * Pure helpers for the editor-link proxy routes
 * (app/api/documents/[documentId]/editor-link/**). No DOM, no `server-only`
 * imports — unit-tested in test/editor-links.test.ts and safe to run on the
 * edge.
 *
 * These mirror lib/documents.ts:whitelistPatchDocument and lib/reader.ts:
 * whitelistPositionUpdate — a STRUCTURAL whitelist that re-serializes ONLY the
 * allowed field upstream. The unlink body's `mode` is the settled mandatory
 * user choice (`pull-final` | `keep-app`); the whitelist drops every other key
 * AND rejects an out-of-set value before the body reaches the API's
 * `extra="forbid"` gate. A client can never smuggle a `provider_file_id`, a
 * `document_id`, a `user_id`, or any other field through the proxy — the proxy
 * forwards exactly `{mode}`, and the API resolves the file id / ownership from
 * the JWT + path alone (the file id is a server-side capability, never a
 * client-supplied authoritative value).
 *
 * The LINK (POST) and STATUS (GET) proxies forward NO body at all, so there is
 * nothing to whitelist for them — the document is identified by the URL path
 * (URL-encoded in the route) and the owner by the cookie-derived bearer.
 */

export type UnlinkBodyResult = { ok: true; body: UnlinkRequest } | { ok: false; error: string };

/** The allowed `mode` values — the settled pull-final-vs-keep-app choice. */
const UNLINK_MODES = ["pull-final", "keep-app"] as const;

/** True iff `value` is one of the two settled unlink modes. */
export function isUnlinkMode(value: unknown): value is UnlinkMode {
  return typeof value === "string" && (UNLINK_MODES as readonly string[]).includes(value);
}

/**
 * Structural whitelist for the unlink proxy body. Forwards ONLY `mode` (one of
 * `pull-final` | `keep-app`); every other key is dropped by constructing a
 * fresh object with just that field, and an absent / non-string / out-of-set
 * `mode` is a 400 here BEFORE the body reaches the API. The pull-final vs
 * keep-app semantics (snapshot+overwrite then unlink, vs leave-content unlink)
 * are the API's to enact — this layer only guarantees the structural shape and
 * the closed value set.
 */
export function whitelistUnlink(body: unknown): UnlinkBodyResult {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return { ok: false, error: "Request body must be a JSON object." };
  }
  const record = body as Record<string, unknown>;
  if (!isUnlinkMode(record.mode)) {
    return { ok: false, error: "mode must be 'pull-final' or 'keep-app'." };
  }
  return { ok: true, body: { mode: record.mode } };
}
