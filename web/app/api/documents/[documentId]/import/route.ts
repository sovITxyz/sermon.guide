import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail, passthroughResponse } from "@/lib/http";
import type { DocumentFull } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Proxy a DOCX import to the API with the bearer from the HttpOnly cookie
 * (Phase 43). The file is re-wrapped into a fresh FormData so `fetch` sets the
 * multipart boundary itself (the `app/api/upload/route.ts` re-wrap pattern).
 * The browser never holds the JWT; the API owns the whole attacker-controlled-
 * upload pipeline (size cap -> 413, libmagic docx sniff -> 415, /tmp staging
 * with cleanup, the owned-document gate, snapshot-first overwrite) and derives
 * the owner from the token. The `documentId` path segment is URL-encoded.
 *
 * POST /documents/{id}/import (multipart `file`). On success the API returns the
 * full updated document (the import OVERWROTE `content`/`content_text` after
 * snapshotting the prior version) — passed back as JSON so the editor reloads
 * its buffer as TipTap JSON (ZERO dangerouslySetInnerHTML).
 *
 * Status passthrough — the API's 4xx/404 must surface to the client:
 * - 404 (non-owned / nonexistent / non-UUID / soft-deleted) is the API's
 *   uniform `{"detail": "Document not found."}` — passed through byte-for-byte so
 *   the no-existence-oracle contract stays the API's alone.
 * - 413 (file over the size cap) and 415 (not a real docx — libmagic sniff)
 *   carry the API's canonical detail; passed through byte-for-byte.
 * - 502 (pandoc/conversion failure — fixed detail, no stack-trace oracle) and
 *   any other non-OK status surface as a generic JSON error.
 */
export async function POST(
  req: Request,
  ctx: { params: Promise<{ documentId: string }> },
): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const form = await req.formData();
  const file = form.get("file");
  if (!(file instanceof File)) {
    return NextResponse.json({ error: "No file provided." }, { status: 400 });
  }

  // Re-wrap so `fetch` owns the multipart boundary; forward ONLY the file (the
  // API takes no body params — owner + document come from the token + path).
  const upstream = new FormData();
  upstream.append("file", file, file.name);

  const { documentId } = await ctx.params;
  const res = await fetch(`${apiBaseUrl()}/documents/${encodeURIComponent(documentId)}/import`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}` },
    body: upstream,
    cache: "no-store",
  });

  // 404 (no-oracle), 413 (too large), and 415 (not a docx) carry the API's
  // canonical detail; pass them through byte-for-byte so the client surfaces the
  // exact reason and the no-oracle contract stays the API's alone.
  if (res.status === 404 || res.status === 413 || res.status === 415) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not import the document.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as DocumentFull;
  return NextResponse.json(data);
}
