import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail, passthroughResponse } from "@/lib/http";
import { NextResponse } from "next/server";

/**
 * Stream a sermon's DOCX export through the same-origin proxy (Phase 43). The
 * bearer comes from the HttpOnly cookie; the browser never holds the JWT and
 * never sees the API origin (Phase 15/16 proxy pattern). The `documentId` path
 * segment is URL-encoded before interpolation.
 *
 * GET /documents/{id}/export.docx returns a binary `.docx` body — NOT JSON — so
 * this handler streams the upstream bytes through verbatim, forwarding the
 * `content-type` (application/vnd.openxmlformats-officedocument.wordprocessingml.document)
 * and the `content-disposition` (the API sanitizes the filename from the
 * user-controlled title — `_export_filename` — so no quote/CR/LF/slash survives
 * the header). We do NOT re-emit the disposition ourselves: it is the API's
 * sanitized header, passed through unmodified.
 *
 * - A non-owned, nonexistent, non-UUID, or soft-deleted id is the API's uniform
 *   `{"detail": "Document not found."}` 404 — passed through byte-for-byte so the
 *   no-existence-oracle contract stays a property of the API alone.
 * - A conversion failure is the API's fixed-detail 502 (no stack-trace oracle);
 *   surfaced as a generic JSON error so the client shows a visible message.
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
  const res = await fetch(
    `${apiBaseUrl()}/documents/${encodeURIComponent(documentId)}/export.docx`,
    {
      headers: { authorization: `Bearer ${token}` },
      cache: "no-store",
    },
  );

  // The uniform 404 carries the API's canonical `{detail}` JSON — pass it
  // through byte-for-byte so the no-oracle contract is the API's alone.
  if (res.status === 404) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not export the sermon.") },
      { status: res.status },
    );
  }

  // Stream the binary docx through, forwarding the content-type and the API's
  // already-sanitized content-disposition (filename). Nothing else.
  const headers = new Headers();
  const contentType = res.headers.get("content-type");
  if (contentType) {
    headers.set("content-type", contentType);
  }
  const disposition = res.headers.get("content-disposition");
  if (disposition) {
    headers.set("content-disposition", disposition);
  }
  return new Response(res.body, { status: res.status, headers });
}
