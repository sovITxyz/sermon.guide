import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail, passthroughResponse } from "@/lib/http";
import type { ChunkWindowResponse } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Proxy GET /books/{book_id}/chunks?start&limit with the bearer from the
 * HttpOnly cookie. Only `start` and `limit` are forwarded, verbatim — the
 * API owns all validation (negative `start` / non-positive `limit` → 422
 * passed through; `limit` over 100 is silently capped upstream, never an
 * error; `start` past the book's end is 200 with an empty `chunks` array).
 *
 * `bookId` is URL-encoded before interpolation so a crafted path segment
 * can't escape the `/books/{id}/chunks` route on the upstream. Upstream
 * 404s pass through byte-for-byte: the API collapses non-UUID, nonexistent,
 * and non-owned ids into one identical `{"detail": "Book not found."}` (no
 * existence oracle), and this proxy does not re-wrap that body.
 */
export async function GET(
  req: Request,
  ctx: { params: Promise<{ bookId: string }> },
): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const { bookId } = await ctx.params;
  const incoming = new URL(req.url).searchParams;
  const forwarded = new URLSearchParams();
  for (const key of ["start", "limit"] as const) {
    const value = incoming.get(key);
    if (value !== null) {
      forwarded.set(key, value);
    }
  }
  const query = forwarded.toString();
  const res = await fetch(
    `${apiBaseUrl()}/books/${encodeURIComponent(bookId)}/chunks${query ? `?${query}` : ""}`,
    {
      headers: { authorization: `Bearer ${token}` },
      cache: "no-store",
    },
  );

  if (res.status === 404) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not load this part of the book.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as ChunkWindowResponse;
  return NextResponse.json(data);
}
