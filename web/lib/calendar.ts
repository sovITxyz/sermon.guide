import type { CalendarEventCreate, CalendarEventPatch } from "./types";

/**
 * Pure structural whitelists for the calendar mutation proxy routes
 * (app/api/sermon-events/**). No DOM, no server-only imports — unit-tested in
 * test/calendar-body.test.ts and safe to run on the edge.
 *
 * These mirror lib/documents.ts:whitelistCreateDocument /
 * whitelistPatchDocument — a STRUCTURAL whitelist that re-serializes ONLY the
 * allowed fields into a FRESH object so nothing else reaches the API.
 * Critically, `document_id` is DROPPED by both whitelists: Phase 40 defers
 * sermon-linking to Phase 41, and forwarding it early would re-open the
 * cross-tenant ownership trap the API closed in Phase 38. The PATCH whitelist
 * also drops `repeat_weekly_until` (not a PATCH field on the API — would trip
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
 * `event_date` (required string) and `title` (required string), plus `series`
 * and `repeat_weekly_until` when present — each a `string | null` (null is a
 * meaningful "no series" / "no repeat" value the client may send). Every other
 * key — including `document_id` — is dropped by building a fresh object. An
 * absent optional field is OMITTED (not sent as null) so the API's defaults
 * apply. Length/range/cap validation is left to the API (422).
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
  return { ok: true, body: out };
}

/**
 * Whitelist for the PATCH /calendar/events/{id} proxy body. Forwards ONLY
 * `event_date`, `title`, and `series` when present; every other key —
 * including `document_id` and `repeat_weekly_until` — is dropped. There is NO
 * required token (unlike documents' `base_updated_at`): calendar events have no
 * optimistic-concurrency field. An absent optional field is OMITTED from the
 * forwarded body (not sent as null) so the API's three-state PATCH semantics
 * see only what the client set; `series: null` is forwarded verbatim to DETACH
 * the series. The at-least-one-of-field rule and length checks are the API's
 * 422 to own — this layer only guarantees the structural shape.
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
  return { ok: true, body: out };
}
