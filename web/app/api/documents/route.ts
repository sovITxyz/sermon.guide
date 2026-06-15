import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { whitelistCreateDocument } from "@/lib/documents";
import { errorDetail, passthroughResponse } from "@/lib/http";
import type { DocumentFull, DocumentListResponse } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Proxy the documents collection with the bearer from the HttpOnly cookie.
 *
 * - GET: list the caller's non-deleted sermons (preview-only items, no
 *   `content`). Per-user data -> `cache: "no-store"`.
 * - POST: create a sermon. The body goes through a STRUCTURAL whitelist
 *   (lib/documents.ts) — only `title` and `content` are re-serialized
 *   upstream; `user_id`/`content_text`/`schema_version`/`document_id` are
 *   dropped before the body reaches the API's `extra="forbid"` gate. Wrong
 *   primitive types are a 400 here; title length and the content byte cap stay
 *   with the API, whose 422/413 passes through.
 *
 * The 201 create response is the full document including its `document_id` so
 * the client can route to /sermons/[id].
 */
export async function GET(): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const res = await fetch(`${apiBaseUrl()}/documents`, {
    headers: { authorization: `Bearer ${token}` },
    cache: "no-store",
  });

  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not load your sermons.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as DocumentListResponse;
  return NextResponse.json(data);
}

export async function POST(req: Request): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const parsed = (await req.json().catch(() => null)) as unknown;
  const result = whitelistCreateDocument(parsed);
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: 400 });
  }

  const res = await fetch(`${apiBaseUrl()}/documents`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(result.body),
    cache: "no-store",
  });

  // 413 (content too large) carries the API's canonical detail; pass it
  // through byte-for-byte so the size-cap contract has one owner.
  if (res.status === 413) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not create the sermon.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as DocumentFull;
  return NextResponse.json(data, { status: 201 });
}
