/**
 * Session-cookie policy. Pure (no `next/headers`, no `server-only`) so it is
 * safe to import from the Edge middleware and from Vitest unit tests alike.
 *
 * The JWT itself lives ONLY inside this HttpOnly cookie — never localStorage,
 * never a client-readable cookie, never the RSC payload. The browser cannot
 * read it; only the route handlers / server components attach it as a bearer
 * when proxying to the API (see lib/api-server.ts, web/AGENTS.md).
 */

export const SESSION_COOKIE = "sg_session";

/**
 * Matches the API's `jwt_ttl_seconds` (api/settings.py, 1 hour). Cookie and
 * token expire together so a stale cookie never outlives the JWT it carries.
 */
export const SESSION_MAX_AGE_SECONDS = 60 * 60;

export interface SessionCookieOptions {
  httpOnly: true;
  secure: boolean;
  sameSite: "lax";
  path: "/";
  maxAge: number;
}

/** Cookie attributes for a freshly-issued session. `secure` is on in prod. */
export function sessionCookieOptions(
  isProduction: boolean = process.env.NODE_ENV === "production",
): SessionCookieOptions {
  return {
    httpOnly: true,
    secure: isProduction,
    // Lax (not None) is sufficient: every authenticated call is same-origin to a
    // Next route handler, and Lax still blocks the cookie on cross-site POSTs.
    sameSite: "lax",
    path: "/",
    maxAge: SESSION_MAX_AGE_SECONDS,
  };
}

/** Same attributes with `maxAge: 0` — expires the cookie immediately (logout). */
export function clearedSessionCookieOptions(
  isProduction: boolean = process.env.NODE_ENV === "production",
): SessionCookieOptions {
  return { ...sessionCookieOptions(isProduction), maxAge: 0 };
}
