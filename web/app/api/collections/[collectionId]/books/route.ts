import { getSessionToken } from "@/lib/api-server";
import { whitelistCollectionBooks } from "@/lib/collections";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail, passthroughResponse } from "@/lib/http";
import type { Collection } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Proxy collection membership with the bearer from the HttpOnly cookie
 * (Phase 48). The `collectionId` is taken from the PATH segment only — never the
 * body — and URL-encoded before interpolation; a non-UUID / nonexistent /
 * cross-tenant id is the API's uniform no-oracle 404. Both methods carry a
 * STRUCTURALLY-whitelisted `{book_ids}` body (lib/collections.ts) — a smuggled
 * `user_id` never reaches the API's `extra="forbid"` gate. Wrong primitive types
 * are a 400 here. The 404 (no oracle) and 422 (length / empty) pass through
 * byte-for-byte; the success body is the collection with its refreshed
 * `book_ids`.
 *
 * - POST: ADD books. The API CLAMPS the requested set to the owner's library
 *   server-side (a foreign/unowned book is silently dropped) and inserts ON
 *   CONFLICT DO NOTHING — this proxy adds NO ownership pre-check, the API owns
 *   the tenant clamp.
 * - DELETE: REMOVE books (the body carries the set to remove — a DELETE WITH a
 *   body, mirroring the API route). Removing a non-member is a no-op.
 */
export async function POST(
  req: Request,
  ctx: { params: Promise<{ collectionId: string }> },
): Promise<Response> {
  return forwardBooks(req, ctx, "POST", "Could not add books to the collection.");
}

export async function DELETE(
  req: Request,
  ctx: { params: Promise<{ collectionId: string }> },
): Promise<Response> {
  return forwardBooks(req, ctx, "DELETE", "Could not remove books from the collection.");
}

async function forwardBooks(
  req: Request,
  ctx: { params: Promise<{ collectionId: string }> },
  method: "POST" | "DELETE",
  fallback: string,
): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const parsed = (await req.json().catch(() => null)) as unknown;
  const result = whitelistCollectionBooks(parsed);
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: 400 });
  }

  const { collectionId } = await ctx.params;
  const res = await fetch(`${apiBaseUrl()}/collections/${encodeURIComponent(collectionId)}/books`, {
    method,
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(result.body),
    cache: "no-store",
  });

  if (res.status === 404 || res.status === 422) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json({ error: await errorDetail(res, fallback) }, { status: res.status });
  }

  const data = (await res.json()) as Collection;
  return NextResponse.json(data);
}
