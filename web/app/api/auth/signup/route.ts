import { apiBaseUrl } from "@/lib/config";
import { clientIpHeaders, errorDetail } from "@/lib/http";
import { NextResponse } from "next/server";

/**
 * Proxy a sign-up to the API. No cookie is set here — the user is sent to
 * /login afterward (a deliberate two-step flow), so signup never mints a
 * session. The browser only ever talks to this same-origin handler.
 */
export async function POST(req: Request): Promise<Response> {
  let body: { email?: unknown; password?: unknown };
  try {
    body = (await req.json()) as { email?: unknown; password?: unknown };
  } catch {
    return NextResponse.json({ error: "Invalid request body." }, { status: 400 });
  }

  // clientIpHeaders: the API's per-IP signup limiter (Phase 19) needs the real
  // client address — without it every browser collapses into this server's IP.
  const res = await fetch(`${apiBaseUrl()}/auth/signup`, {
    method: "POST",
    headers: { "content-type": "application/json", ...clientIpHeaders(req) },
    body: JSON.stringify({ email: body.email, password: body.password }),
    cache: "no-store",
  });

  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Sign-up failed.") },
      { status: res.status },
    );
  }
  return NextResponse.json({ ok: true }, { status: 201 });
}
