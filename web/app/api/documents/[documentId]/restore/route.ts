import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail, passthroughResponse } from "@/lib/http";
import type { DocumentFull } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Proxy POST /documents/{id}/restore with the bearer from the HttpOnly cookie.
 * Clears `deleted_at`; idempotent on an already-active doc; the uniform
 * `{"detail": "Document not found."}` 404 (non-owned / nonexistent / non-UUID)
 * passes through byte-for-byte so the no-existence-oracle contract stays with
 * the API. There is no request body — restore takes only the path id.
 */
export async function POST(
  _req: Request,
  ctx: { params: Promise<{ documentId: string }> },
): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const { documentId } = await ctx.params;
  const res = await fetch(`${apiBaseUrl()}/documents/${encodeURIComponent(documentId)}/restore`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}` },
    cache: "no-store",
  });

  if (res.status === 404) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not restore the sermon.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as DocumentFull;
  return NextResponse.json(data);
}
