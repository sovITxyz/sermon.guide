import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail } from "@/lib/http";
import type { UploadAccepted } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Proxy a multipart upload to the API with the bearer from the HttpOnly cookie.
 * The file is re-wrapped into a fresh FormData so `fetch` sets the multipart
 * boundary itself. The browser never holds the JWT; the API enforces the
 * size cap and derives the owner from the token (api/uploads.py).
 */
export async function POST(req: Request): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const form = await req.formData();
  const file = form.get("file");
  if (!(file instanceof File)) {
    return NextResponse.json({ error: "No file provided." }, { status: 400 });
  }

  const upstream = new FormData();
  upstream.append("file", file, file.name);

  const res = await fetch(`${apiBaseUrl()}/upload`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}` },
    body: upstream,
    cache: "no-store",
  });

  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Upload failed.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as UploadAccepted;
  return NextResponse.json(data, { status: 202 });
}
