import { apiBaseUrl } from "@/lib/config";
import { clientIpHeaders, errorDetail } from "@/lib/http";
import { SESSION_COOKIE, sessionCookieOptions } from "@/lib/session";
import { NextResponse } from "next/server";

/**
 * Proxy a login to the API and, on success, plant the returned JWT in an
 * HttpOnly cookie. The token is set here and read back only by server code
 * (route handlers / server components) — it is never returned in the response
 * body, so client JS never sees it.
 */
export async function POST(req: Request): Promise<Response> {
  let body: { email?: unknown; password?: unknown };
  try {
    body = (await req.json()) as { email?: unknown; password?: unknown };
  } catch {
    return NextResponse.json({ error: "Invalid request body." }, { status: 400 });
  }

  // clientIpHeaders: the API's per-IP login limiter (Phase 19) needs the real
  // client address — without it every browser collapses into this server's IP.
  const res = await fetch(`${apiBaseUrl()}/auth/login`, {
    method: "POST",
    headers: { "content-type": "application/json", ...clientIpHeaders(req) },
    body: JSON.stringify({ email: body.email, password: body.password }),
    cache: "no-store",
  });

  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Invalid credentials.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as { access_token?: unknown };
  if (typeof data.access_token !== "string") {
    return NextResponse.json({ error: "Unexpected auth response." }, { status: 502 });
  }

  const out = NextResponse.json({ ok: true });
  out.cookies.set(SESSION_COOKIE, data.access_token, sessionCookieOptions());
  return out;
}
