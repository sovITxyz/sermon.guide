import { getSessionToken } from "@/lib/api-server";
import { whitelistCreateCollection } from "@/lib/collections";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail, passthroughResponse } from "@/lib/http";
import type { Collection, CollectionListResponse } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Proxy GET /collections with the bearer from the HttpOnly cookie (Phase 48).
 * The endpoint has no path id, so it never 404s; the API resolves the caller's
 * collections from the JWT `user_id` server-side (this proxy adds no scoping
 * predicate of its own). A non-OK upstream status surfaces the FastAPI
 * `{detail}` as `{error}`. Per-user data → `cache: "no-store"`. Mirrors
 * app/api/sermon-events/route.ts.
 */
export async function GET(): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const res = await fetch(`${apiBaseUrl()}/collections`, {
    headers: { authorization: `Bearer ${token}` },
    cache: "no-store",
  });

  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not load your collections.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as CollectionListResponse;
  return NextResponse.json(data);
}

/**
 * Create a collection (Phase 48). The body goes through a STRUCTURAL whitelist
 * (lib/collections.ts) — only `name` and the optional `description` are
 * re-serialized upstream before the body reaches the API's `extra="forbid"`
 * gate. Wrong primitive types are a 400 here. All length validation (name
 * 1..255, description <= 2000) is the API's 422 to own: this proxy does NOT
 * pre-check it and passes the 422 through byte-for-byte. The 201 body is the
 * created Collection (empty `book_ids`). Per-user write → `cache: "no-store"`.
 */
export async function POST(req: Request): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const parsed = (await req.json().catch(() => null)) as unknown;
  const result = whitelistCreateCollection(parsed);
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: 400 });
  }

  const res = await fetch(`${apiBaseUrl()}/collections`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(result.body),
    cache: "no-store",
  });

  if (res.status === 422) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not create the collection.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as Collection;
  return NextResponse.json(data, { status: 201 });
}
