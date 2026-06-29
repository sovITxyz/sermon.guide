import { getSessionToken } from "@/lib/api-server";
import { whitelistCreateEvent } from "@/lib/calendar";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail, passthroughResponse } from "@/lib/http";
import type { CalendarEventListResponse } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Proxy GET /calendar/events?start&end with the bearer from the HttpOnly
 * cookie (Phase 39). Only `start` and `end` are forwarded — built into a FRESH
 * URLSearchParams, copied verbatim, so nothing else from the incoming query
 * string ever reaches upstream (the structural-whitelist invariant; never
 * forward `req.url`'s params wholesale).
 *
 * All range validation belongs to the API alone: `start <= end`, and the span
 * `(end - start).days <= RANGE_CAP_DAYS` (400), are 422s the API owns — this
 * proxy does NOT pre-check them so there is a single owner of the contract. A
 * full year (≤ 366 days) fits in one call. The endpoint has no path id, so it
 * never 404s; non-OK upstream statuses surface the FastAPI `{detail}` as
 * `{error}`. Per-user data → `cache: "no-store"`.
 */
export async function GET(req: Request): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const incoming = new URL(req.url).searchParams;
  const forwarded = new URLSearchParams();
  for (const key of ["start", "end"] as const) {
    const value = incoming.get(key);
    if (value !== null) {
      forwarded.set(key, value);
    }
  }
  const query = forwarded.toString();
  const res = await fetch(`${apiBaseUrl()}/calendar/events${query ? `?${query}` : ""}`, {
    headers: { authorization: `Bearer ${token}` },
    cache: "no-store",
  });

  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not load the calendar.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as CalendarEventListResponse;
  return NextResponse.json(data);
}

/**
 * Create one or more sermon events (Phase 40). The body goes through a
 * STRUCTURAL whitelist (lib/calendar.ts) — only `event_date`, `title`,
 * `series`, `repeat_weekly_until`, and `document_id` are re-serialized upstream
 * before the body reaches the API's `extra="forbid"` gate. `document_id`
 * (Phase 47 schedule-from-sermon) is forwarded verbatim so the editor can create
 * an event already linked to a sermon in one POST; the API ownership-checks a
 * non-null value (no-oracle 404). Wrong primitive types are a 400 here.
 *
 * The 201 response is a LIST (`{ events }`) even for a single create — a
 * `repeat_weekly_until` materializes many independent rows on the API side, so
 * the client must merge ALL returned events. All length/range/cap validation
 * (title length, `repeat_weekly_until >= event_date`, the 53-row materializer
 * cap) is the API's 422 to own: this proxy does NOT pre-check it and passes the
 * 422 through byte-for-byte. Per-user write → `cache: "no-store"`.
 */
export async function POST(req: Request): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const parsed = (await req.json().catch(() => null)) as unknown;
  const result = whitelistCreateEvent(parsed);
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: 400 });
  }

  const res = await fetch(`${apiBaseUrl()}/calendar/events`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(result.body),
    cache: "no-store",
  });

  // 422 (range / cap / length) carries the API's canonical detail; pass it
  // through byte-for-byte so the materializer-cap contract has one owner.
  if (res.status === 422) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not create the event.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as CalendarEventListResponse;
  return NextResponse.json(data, { status: 201 });
}
