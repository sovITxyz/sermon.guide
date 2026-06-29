import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail, passthroughResponse } from "@/lib/http";
import type { SearchHistoryEntry } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Proxy a single saved search with the bearer from the HttpOnly cookie
 * (Phase 51). The `historyId` is taken from the PATH segment only — never the
 * body — and URL-encoded before interpolation. The API treats a non-UUID /
 * nonexistent / cross-tenant id as its uniform
 * `{"detail": "Search history entry not found."}` 404 (no existence oracle), so
 * encoding an arbitrary segment is safe.
 *
 * - GET: the FULL saved search INCLUDING `result` (the replayable
 *   SummaryResponse) so the Recent panel rehydrates SearchPanel's summary +
 *   citation render with NO second /search-summary call. The 404 (no oracle)
 *   passes through byte-for-byte.
 * - DELETE: HARD delete (204 on success, no body). The same uniform 404 on a
 *   non-owned / nonexistent / non-UUID id passes through.
 *
 * Per-user data -> `cache: "no-store"`. Mirrors
 * app/api/sermon-events/[eventId]/route.ts.
 */
export async function GET(
  _req: Request,
  ctx: { params: Promise<{ historyId: string }> },
): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const { historyId } = await ctx.params;
  const res = await fetch(`${apiBaseUrl()}/search-history/${encodeURIComponent(historyId)}`, {
    headers: { authorization: `Bearer ${token}` },
    cache: "no-store",
  });

  if (res.status === 404) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not open that search.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as SearchHistoryEntry;
  return NextResponse.json(data);
}

export async function DELETE(
  _req: Request,
  ctx: { params: Promise<{ historyId: string }> },
): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const { historyId } = await ctx.params;
  const res = await fetch(`${apiBaseUrl()}/search-history/${encodeURIComponent(historyId)}`, {
    method: "DELETE",
    headers: { authorization: `Bearer ${token}` },
    cache: "no-store",
  });

  if (res.status === 404) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not delete that search.") },
      { status: res.status },
    );
  }

  // 204 No Content — no body to forward.
  return new Response(null, { status: 204 });
}
