import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail, passthroughResponse } from "@/lib/http";
import type { EditorLinkStatus } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Link a sermon to an external editor (Google Docs) — Phase 45, B4. Proxies the
 * LINK request to the API with the bearer from the HttpOnly cookie. The
 * `documentId` path segment is URL-encoded before interpolation.
 *
 * POST /documents/{id}/editor-link — NO request body is forwarded: the document
 * is identified by the URL path and the owner by the cookie-derived bearer; the
 * provider is fixed (`google`) and the OAuth connection is resolved server-side
 * from the JWT. The client can NOT supply a `provider_file_id` or any other
 * field — the API converts the sermon to a Doc, creates it in the user's Drive,
 * and returns `{state, web_url, remote_changed}` (NO token, NO file id ever
 * reaches the browser; `web_url` is the only external string).
 *
 * Status passthrough — the API's load-bearing 4xx must surface verbatim:
 * - 404 (non-owned / nonexistent / non-UUID / soft-deleted) is the API's
 *   uniform `{"detail": "Document not found."}` — passed through byte-for-byte so
 *   the no-existence-oracle contract stays the API's alone.
 * - 409 (already linked to an external editor — the partial-unique backstop)
 *   carries the API's canonical detail; passed through so the editor can surface
 *   the already-linked reason.
 * - 400 (Google not connected — the API tells the user to connect first; a
 *   no-oracle status, NOT a 404 on the doc) carries the API's detail.
 * - 502 (Drive/conversion failure) and 503 (OAuth unconfigured) surface as a
 *   generic JSON error — the API stays the only oracle, no detail leak beyond
 *   its own `{detail}`.
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
  const res = await fetch(
    `${apiBaseUrl()}/documents/${encodeURIComponent(documentId)}/editor-link`,
    {
      method: "POST",
      headers: { authorization: `Bearer ${token}` },
      cache: "no-store",
    },
  );

  // 404 (no-oracle), 409 (already linked), and 400 (connect Google first) carry
  // the API's canonical detail; pass them through byte-for-byte.
  if (res.status === 404 || res.status === 409 || res.status === 400) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not link the sermon to Google Docs.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as EditorLinkStatus;
  return NextResponse.json(data);
}
