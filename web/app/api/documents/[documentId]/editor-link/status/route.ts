import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail, passthroughResponse } from "@/lib/http";
import type { EditorLinkStatus } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Poll the external-editor link status for one sermon (Phase 45). Proxies the
 * GET with the bearer from the HttpOnly cookie. The `documentId` path segment
 * is URL-encoded before interpolation. NO request body.
 *
 * GET /documents/{id}/editor-link/status returns `{state, web_url,
 * remote_changed}` for the JWT user's OWN document — the API compares the
 * stored remote-version cursor against Drive (the cursor never crosses the
 * wire) to set `remote_changed`, and flips `state` to `error` if the Google
 * refresh token has expired (the re-connect prompt). NO token / file id / cursor
 * material is in the payload.
 *
 * - 404 (non-owned / nonexistent / non-UUID / soft-deleted, OR no active link)
 *   is the API's uniform `{detail}` — passed through byte-for-byte (no oracle).
 * - Any other non-OK status surfaces as a generic JSON error.
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
    `${apiBaseUrl()}/documents/${encodeURIComponent(documentId)}/editor-link/status`,
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
      { error: await errorDetail(res, "Could not check the Google Docs link.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as EditorLinkStatus;
  return NextResponse.json(data);
}
