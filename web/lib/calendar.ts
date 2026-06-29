import type { CalendarEventCreate, CalendarEventPatch } from "./types";

/**
 * Pure structural whitelists for the calendar mutation proxy routes
 * (app/api/sermon-events/**). No DOM, no server-only imports — unit-tested in
 * test/calendar-body.test.ts and safe to run on the edge.
 *
 * These mirror lib/documents.ts:whitelistCreateDocument /
 * whitelistPatchDocument — a STRUCTURAL whitelist that re-serializes ONLY the
 * allowed fields into a FRESH object so nothing else reaches the API. The
 * CREATE whitelist now FORWARDS `document_id` (Phase 47 schedule-from-sermon):
 * the editor schedules a sermon by creating an event already linked to it in one
 * POST (the sermon doc always exists, so the calendar-first POST-then-PATCH
 * two-step is unnecessary). Forwarding the value verbatim is safe for the same
 * reason as the PATCH path — the API ownership-checks a non-null `document_id`
 * and returns a no-oracle 404 on a cross-tenant/nonexistent id. The PATCH
 * whitelist FORWARDS `document_id` with the same three-state semantics as
 * `series` (Phase 41 sermon-linking):
 * present-and-string re-links, present-and-null detaches, absent leaves it
 * alone. Forwarding the value verbatim is safe because the API ownership-checks
 * a non-null `document_id` against the JWT user's documents (Phase 38,
 * `model_fields_set`) and returns a no-oracle 404 on a cross-tenant/nonexistent
 * id — the proxy must NOT swallow or pre-validate that. The PATCH whitelist
 * still drops `repeat_weekly_until` (not a PATCH field on the API — would trip
 * `extra="forbid"` with a 422).
 *
 * Checks here are STRUCTURAL only (object-ness + primitive types). Every
 * length/range/cap/at-least-one-of rule stays with the API so the 422 contract
 * has exactly one owner (api/calendar_routes.py): title 1..512, series <= 512,
 * `repeat_weekly_until >= event_date`, the 53-row materializer cap, and the
 * "PATCH must set at least one field" rule.
 */

export type CalendarBodyResult<T> = { ok: true; body: T } | { ok: false; error: string };

/** Reject anything that is not a plain JSON object (arrays and null included). */
function asRecord(body: unknown): Record<string, unknown> | null {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return null;
  }
  return body as Record<string, unknown>;
}

/**
 * Whitelist for the POST /calendar/events proxy body. Forwards ONLY
 * `event_date` (required string) and `title` (required string), plus `series`,
 * `repeat_weekly_until`, and `document_id` when present — each a `string | null`
 * (null is a meaningful "no series" / "no repeat" / "no link" value the client
 * may send). Every other key is dropped by building a fresh object. An absent
 * optional field is OMITTED (not sent as null) so the API's defaults apply. A
 * non-null `document_id` (Phase 47) is forwarded as-is: the API ownership-checks
 * it against the JWT user's documents and returns a no-oracle 404 on a
 * cross-tenant/nonexistent id, so the proxy must NOT pre-validate it here.
 * Length/range/cap validation is left to the API (422).
 */
export function whitelistCreateEvent(body: unknown): CalendarBodyResult<CalendarEventCreate> {
  const record = asRecord(body);
  if (record === null) {
    return { ok: false, error: "Request body must be a JSON object." };
  }
  if (typeof record.event_date !== "string") {
    return { ok: false, error: "event_date must be a string." };
  }
  if (typeof record.title !== "string") {
    return { ok: false, error: "title must be a string." };
  }
  const out: CalendarEventCreate = { event_date: record.event_date, title: record.title };
  if (record.series !== undefined) {
    if (record.series !== null && typeof record.series !== "string") {
      return { ok: false, error: "series must be a string or null." };
    }
    out.series = record.series;
  }
  if (record.repeat_weekly_until !== undefined) {
    if (record.repeat_weekly_until !== null && typeof record.repeat_weekly_until !== "string") {
      return { ok: false, error: "repeat_weekly_until must be a string or null." };
    }
    out.repeat_weekly_until = record.repeat_weekly_until;
  }
  if (record.document_id !== undefined) {
    if (record.document_id !== null && typeof record.document_id !== "string") {
      return { ok: false, error: "document_id must be a string or null." };
    }
    out.document_id = record.document_id;
  }
  return { ok: true, body: out };
}

/**
 * Whitelist for the PATCH /calendar/events/{id} proxy body. Forwards ONLY
 * `event_date`, `title`, `series`, and `document_id` when present; every other
 * key — including `repeat_weekly_until` — is dropped. There is NO required
 * token (unlike documents' `base_updated_at`): calendar events have no
 * optimistic-concurrency field. An absent optional field is OMITTED from the
 * forwarded body (not sent as null) so the API's three-state PATCH semantics
 * see only what the client set; `series: null` and `document_id: null` are each
 * forwarded VERBATIM to DETACH the series / UNLINK the sermon. The check is
 * pure key-presence (`!== undefined`), NEVER truthiness — a present `null` must
 * survive to the API (a truthiness guard would silently drop the unlink). A
 * non-null `document_id` is forwarded as-is: the API (Phase 38) ownership-checks
 * it against the JWT user's documents and returns a no-oracle 404 on a
 * cross-tenant/nonexistent id, so the proxy must NOT pre-validate it here. The
 * at-least-one-of-field rule and length checks are the API's 422 to own — this
 * layer only guarantees the structural shape.
 */
export function whitelistPatchEvent(body: unknown): CalendarBodyResult<CalendarEventPatch> {
  const record = asRecord(body);
  if (record === null) {
    return { ok: false, error: "Request body must be a JSON object." };
  }
  const out: CalendarEventPatch = {};
  if (record.event_date !== undefined) {
    if (typeof record.event_date !== "string") {
      return { ok: false, error: "event_date must be a string." };
    }
    out.event_date = record.event_date;
  }
  if (record.title !== undefined) {
    if (typeof record.title !== "string") {
      return { ok: false, error: "title must be a string." };
    }
    out.title = record.title;
  }
  if (record.series !== undefined) {
    if (record.series !== null && typeof record.series !== "string") {
      return { ok: false, error: "series must be a string or null." };
    }
    out.series = record.series;
  }
  if (record.document_id !== undefined) {
    if (record.document_id !== null && typeof record.document_id !== "string") {
      return { ok: false, error: "document_id must be a string or null." };
    }
    out.document_id = record.document_id;
  }
  return { ok: true, body: out };
}
