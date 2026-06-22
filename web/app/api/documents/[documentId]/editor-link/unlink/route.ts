import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { whitelistUnlink } from "@/lib/editor-links";
import { errorDetail, passthroughResponse } from "@/lib/http";
import type { EditorLinkStatus } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Unlink a sermon from its external editor (Phase 45). Proxies the POST with the
 * bearer from the HttpOnly cookie. The `documentId` path segment is URL-encoded
 * before interpolation.
 *
 * POST /documents/{id}/editor-link/unlink — the ONLY field forwarded is the
 * settled mandatory `mode` choice (`pull-final` | `keep-app`), run through a
 * STRUCTURAL whitelist (lib/editor-links.ts): every other key is dropped and an
 * out-of-set value is a 400 here BEFORE the body reaches the API's
 * `extra="forbid"` gate. `pull-final` runs the pull pipeline once
 * (snapshot+overwrite) then unlinks; `keep-app` leaves the app content untouched
 * and unlinks. The API resolves the file id from the user's OWN row (a
 * server-side capability — the client never supplies one) and best-effort
 * deletes the app-created Doc. Returns `{state: "unlinked", ...}`.
 *
 * Status passthrough:
 * - 404 (non-owned / nonexistent / soft-deleted / no active link) is the API's
 *   uniform `{detail}` — passed through byte-for-byte (no oracle).
 * - 422 (a smuggled extra field tripping the API's `extra="forbid"`, if one ever
 *   slips past the whitelist) carries the API's detail.
 * - any other non-OK status surfaces as a generic JSON error.
 */
export async function POST(
  req: Request,
  ctx: { params: Promise<{ documentId: string }> },
): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const parsed = (await req.json().catch(() => null)) as unknown;
  const result = whitelistUnlink(parsed);
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: 400 });
  }

  const { documentId } = await ctx.params;
  const res = await fetch(
    `${apiBaseUrl()}/documents/${encodeURIComponent(documentId)}/editor-link/unlink`,
    {
      method: "POST",
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
      body: JSON.stringify(result.body),
      cache: "no-store",
    },
  );

  if (res.status === 404 || res.status === 422) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not unlink from Google Docs.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as EditorLinkStatus;
  return NextResponse.json(data);
}
