import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { whitelistPatchDocument } from "@/lib/documents";
import { errorDetail, passthroughResponse } from "@/lib/http";
import type { DocumentFull } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Proxy a single document with the bearer from the HttpOnly cookie. The
 * `documentId` path segment is URL-encoded before interpolation.
 *
 * - GET: the full document (incl. `content`). A non-owned, nonexistent,
 *   non-UUID, or soft-deleted id is the API's uniform
 *   `{"detail": "Document not found."}` 404 — passed through byte-for-byte so
 *   the no-existence-oracle contract stays a property of the API alone.
 * - PATCH: explicit save. The body goes through a STRUCTURAL whitelist
 *   (lib/documents.ts) — only `title`, `content`, and `base_updated_at` are
 *   re-serialized upstream; `user_id`/`content_text`/`schema_version`/
 *   `document_id`/`deleted_at` are dropped before the body reaches the API's
 *   `extra="forbid"` gate. Wrong primitive types are a 400 here. The 409
 *   (stale `base_updated_at`), 404 (no oracle), and 413 (content too large)
 *   all pass through byte-for-byte; title length and the at-least-one-of rule
 *   stay with the API, whose 422 passes through.
 * - DELETE: soft delete (204 on success); the same uniform 404 on a
 *   non-owned / already-deleted id passes through.
 */
export async function GET(
  _req: Request,
  ctx: { params: Promise<{ documentId: string }> },
): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const { documentId } = await ctx.params;
  const res = await fetch(`${apiBaseUrl()}/documents/${encodeURIComponent(documentId)}`, {
    headers: { authorization: `Bearer ${token}` },
    cache: "no-store",
  });

  if (res.status === 404) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not load the sermon.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as DocumentFull;
  return NextResponse.json(data);
}

export async function PATCH(
  req: Request,
  ctx: { params: Promise<{ documentId: string }> },
): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const parsed = (await req.json().catch(() => null)) as unknown;
  const result = whitelistPatchDocument(parsed);
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: 400 });
  }

  const { documentId } = await ctx.params;
  const res = await fetch(`${apiBaseUrl()}/documents/${encodeURIComponent(documentId)}`, {
    method: "PATCH",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(result.body),
    cache: "no-store",
  });

  // 404 (no-oracle) and 413 (content too large) carry the API's canonical
  // detail; pass them through byte-for-byte. 409 (stale base_updated_at) keeps
  // the API's detail too so the client can surface the conflict.
  if (res.status === 404 || res.status === 409 || res.status === 413) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not save the sermon.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as DocumentFull;
  return NextResponse.json(data);
}

export async function DELETE(
  _req: Request,
  ctx: { params: Promise<{ documentId: string }> },
): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const { documentId } = await ctx.params;
  const res = await fetch(`${apiBaseUrl()}/documents/${encodeURIComponent(documentId)}`, {
    method: "DELETE",
    headers: { authorization: `Bearer ${token}` },
    cache: "no-store",
  });

  if (res.status === 404) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not delete the sermon.") },
      { status: res.status },
    );
  }

  // 204 No Content — no body to forward.
  return new Response(null, { status: 204 });
}
