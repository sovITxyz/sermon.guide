import { SESSION_COOKIE } from "@/lib/session";
import { type NextRequest, NextResponse } from "next/server";

/**
 * Presence-only auth gate. If there is no session cookie, bounce to /login
 * before a protected page renders. The cookie is opaque here (HttpOnly, and
 * only the API can verify the HS256 signature), so this is a cheap first pass —
 * full validation happens when the page/route calls the API, which returns 401
 * on an expired or forged token and triggers a redirect there.
 */
export function middleware(req: NextRequest): NextResponse {
  const hasSession = Boolean(req.cookies.get(SESSION_COOKIE)?.value);
  if (!hasSession) {
    const url = req.nextUrl.clone();
    url.pathname = "/login";
    url.searchParams.set("next", req.nextUrl.pathname);
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  matcher: [
    "/library/:path*",
    "/read/:path*",
    "/search/:path*",
    "/upload/:path*",
    "/sermons/:path*",
  ],
};
