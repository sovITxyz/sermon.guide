import { getSessionToken } from "@/lib/api-server";
import { whitelistPatchEvent } from "@/lib/calendar";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail, passthroughResponse } from "@/lib/http";
import type { CalendarEvent } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Proxy a single sermon event with the bearer from the HttpOnly cookie
 * (Phase 40). The `eventId` is taken from the PATH segment only — never the
 * body — and URL-encoded before interpolation. The API treats a non-UUID id as
 * its uniform `{"detail": "Event not found."}` 404 (no existence oracle), so
 * encoding an arbitrary segment is safe.
 *
 * - PATCH: edit. The body goes through a STRUCTURAL whitelist (lib/calendar.ts)
 *   — only `event_date`, `title`, and `series` are re-serialized upstream;
 *   `document_id` (deferred to Phase 41) and `repeat_weekly_until` (not a PATCH
 *   field — would trip the API's `extra="forbid"`) are dropped before the body
 *   reaches the API. Wrong primitive types are a 400 here. The 404 (no oracle)
 *   and 422 (empty patch / length) pass through byte-for-byte; the success body
 *   is a SINGLE updated CalendarEvent.
 * - DELETE: hard delete (204 on success, no body); the same uniform 404 on a
 *   non-owned / nonexistent / non-UUID id passes through.
 */
export async function PATCH(
  req: Request,
  ctx: { params: Promise<{ eventId: string }> },
): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const parsed = (await req.json().catch(() => null)) as unknown;
  const result = whitelistPatchEvent(parsed);
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: 400 });
  }

  const { eventId } = await ctx.params;
  const res = await fetch(`${apiBaseUrl()}/calendar/events/${encodeURIComponent(eventId)}`, {
    method: "PATCH",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(result.body),
    cache: "no-store",
  });

  // 404 (no-oracle) and 422 (empty patch / length) carry the API's canonical
  // detail; pass them through byte-for-byte so those contracts have one owner.
  if (res.status === 404 || res.status === 422) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not save the event.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as CalendarEvent;
  return NextResponse.json(data);
}

export async function DELETE(
  _req: Request,
  ctx: { params: Promise<{ eventId: string }> },
): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const { eventId } = await ctx.params;
  const res = await fetch(`${apiBaseUrl()}/calendar/events/${encodeURIComponent(eventId)}`, {
    method: "DELETE",
    headers: { authorization: `Bearer ${token}` },
    cache: "no-store",
  });

  if (res.status === 404) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not delete the event.") },
      { status: res.status },
    );
  }

  // 204 No Content — no body to forward.
  return new Response(null, { status: 204 });
}
