import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail, passthroughResponse } from "@/lib/http";
import type { DocumentFull } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Pull the latest edits from the linked Google Doc back into the sermon
 * (Phase 45). Proxies the POST with the bearer from the HttpOnly cookie. The
 * `documentId` path segment is URL-encoded before interpolation. NO request
 * body — the document + owner come from the path + token, and the file id is a
 * server-side capability the API resolves from the user's OWN editor-link row
 * (the client never supplies one).
 *
 * POST /documents/{id}/editor-link/pull — the API runs the markdown pull
 * pipeline in ONE transaction (snapshot the current content to a revision FIRST,
 * then overwrite from the Doc's markdown export, re-derive content_text
 * server-side, and bump the remote-version cursor) and returns the full updated
 * document. The editor reloads its buffer with the returned TipTap JSON exactly
 * like the import flow (ZERO dangerouslySetInnerHTML).
 *
 * Status passthrough:
 * - 404 (non-owned / nonexistent / soft-deleted / no active link) is the API's
 *   uniform `{detail}` — passed through byte-for-byte (no oracle).
 * - 413 (the exported markdown exceeds the content cap) carries the API's detail.
 * - 502 (Drive export / pandoc conversion failure) and any other non-OK status
 *   surface as a generic JSON error.
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
    `${apiBaseUrl()}/documents/${encodeURIComponent(documentId)}/editor-link/pull`,
    {
      method: "POST",
      headers: { authorization: `Bearer ${token}` },
      cache: "no-store",
    },
  );

  if (res.status === 404 || res.status === 413) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not pull changes from Google Docs.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as DocumentFull;
  return NextResponse.json(data);
}
