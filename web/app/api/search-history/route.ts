import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail } from "@/lib/http";
import type { SearchHistoryListResponse } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Proxy GET /search-history with the bearer from the HttpOnly cookie (Phase 51).
 * The endpoint has no path id and takes no body, so it never 404s and there is
 * nothing to whitelist; the API resolves the caller's saved searches from the
 * JWT `user_id` server-side (this proxy adds no scoping predicate of its own).
 * The list is LIGHTWEIGHT — query + scope + a short summary preview + timestamp,
 * never the full `result`/citations blob (the per-id GET serves that). A non-OK
 * upstream status surfaces the FastAPI `{detail}` as `{error}`. Per-user data ->
 * `cache: "no-store"`. Mirrors app/api/collections/route.ts (GET).
 */
export async function GET(): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const res = await fetch(`${apiBaseUrl()}/search-history`, {
    headers: { authorization: `Bearer ${token}` },
    cache: "no-store",
  });

  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not load your recent searches.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as SearchHistoryListResponse;
  return NextResponse.json(data);
}
