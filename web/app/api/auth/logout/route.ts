import { SESSION_COOKIE, clearedSessionCookieOptions } from "@/lib/session";
import { NextResponse } from "next/server";

/** Clear the session cookie. Idempotent — safe to call without a session. */
export async function POST(): Promise<Response> {
  const out = NextResponse.json({ ok: true });
  out.cookies.set(SESSION_COOKIE, "", clearedSessionCookieOptions());
  return out;
}
