import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail, passthroughResponse } from "@/lib/http";
import { whitelistPositionUpdate } from "@/lib/reader";
import type { PositionResponse } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Proxy GET/PUT /books/{book_id}/position with the bearer from the HttpOnly
 * cookie.
 *
 * - GET: "no saved position yet" is 200 with null fields, never 404.
 * - PUT: the body goes through a STRUCTURAL whitelist (lib/reader.ts) — only
 *   `chunk_index` and `offset_ratio` are re-serialized upstream, unknown keys
 *   are dropped, wrong primitive types are a 400 here; range validation
 *   (`chunk_index >= 0`, `offset_ratio` 0.0–1.0) stays with the API, whose
 *   422 passes through.
 * - Both: `bookId` is URL-encoded before interpolation, and upstream 404s
 *   pass through byte-for-byte — the API's uniform
 *   `{"detail": "Book not found."}` (no existence oracle) is not re-wrapped.
 */
export async function GET(
  _req: Request,
  ctx: { params: Promise<{ bookId: string }> },
): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const { bookId } = await ctx.params;
  const res = await fetch(`${apiBaseUrl()}/books/${encodeURIComponent(bookId)}/position`, {
    headers: { authorization: `Bearer ${token}` },
    cache: "no-store",
  });

  if (res.status === 404) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not load your reading position.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as PositionResponse;
  return NextResponse.json(data);
}

export async function PUT(
  req: Request,
  ctx: { params: Promise<{ bookId: string }> },
): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const parsed = (await req.json().catch(() => null)) as unknown;
  const result = whitelistPositionUpdate(parsed);
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: 400 });
  }

  const { bookId } = await ctx.params;
  const res = await fetch(`${apiBaseUrl()}/books/${encodeURIComponent(bookId)}/position`, {
    method: "PUT",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(result.body),
    cache: "no-store",
  });

  if (res.status === 404) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not save your reading position.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as PositionResponse;
  return NextResponse.json(data);
}
