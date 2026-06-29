import { getSessionToken } from "@/lib/api-server";
import { whitelistPatchCollection } from "@/lib/collections";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail, passthroughResponse } from "@/lib/http";
import type { Collection } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Proxy a single collection with the bearer from the HttpOnly cookie (Phase 48).
 * The `collectionId` is taken from the PATH segment only — never the body — and
 * URL-encoded before interpolation. The API treats a non-UUID / nonexistent /
 * cross-tenant id as its uniform `{"detail": "Collection not found."}` 404 (no
 * existence oracle), so encoding an arbitrary segment is safe.
 *
 * - PATCH: rename / edit. The body goes through a STRUCTURAL whitelist
 *   (lib/collections.ts) — only `name` and `description` are re-serialized
 *   upstream. Wrong primitive types are a 400 here. The 404 (no oracle) and 422
 *   (empty patch / null name / length) pass through byte-for-byte; the success
 *   body is the updated Collection.
 * - DELETE: HARD delete (204 on success, no body; memberships cascade on the
 *   API). The same uniform 404 on a non-owned / nonexistent / non-UUID id passes
 *   through. Mirrors app/api/sermon-events/[eventId]/route.ts.
 */
export async function PATCH(
  req: Request,
  ctx: { params: Promise<{ collectionId: string }> },
): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const parsed = (await req.json().catch(() => null)) as unknown;
  const result = whitelistPatchCollection(parsed);
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: 400 });
  }

  const { collectionId } = await ctx.params;
  const res = await fetch(`${apiBaseUrl()}/collections/${encodeURIComponent(collectionId)}`, {
    method: "PATCH",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(result.body),
    cache: "no-store",
  });

  if (res.status === 404 || res.status === 422) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not save the collection.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as Collection;
  return NextResponse.json(data);
}

export async function DELETE(
  _req: Request,
  ctx: { params: Promise<{ collectionId: string }> },
): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const { collectionId } = await ctx.params;
  const res = await fetch(`${apiBaseUrl()}/collections/${encodeURIComponent(collectionId)}`, {
    method: "DELETE",
    headers: { authorization: `Bearer ${token}` },
    cache: "no-store",
  });

  if (res.status === 404) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not delete the collection.") },
      { status: res.status },
    );
  }

  // 204 No Content — no body to forward.
  return new Response(null, { status: 204 });
}
