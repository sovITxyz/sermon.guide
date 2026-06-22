import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { isAllowedProvider, settingsRedirectPath } from "@/lib/integrations";
import { NextResponse } from "next/server";

/**
 * The PUBLIC, operator-registered OAuth redirect URI (Phase 44). This is the
 * path the provider top-level-redirects the browser to after consent — it is
 * registered in the Google project as `/api/integrations/google/callback` on
 * the WEB origin (ports 3000 AND 3001; Microsoft mirrors as
 * `/api/integrations/microsoft/callback` in Phase 46, hence the generic
 * `{provider}` segment).
 *
 * Because the provider performs a TOP-LEVEL navigation to this same web origin,
 * the SameSite=Lax `sg_session` cookie rides along, so getSessionToken() works
 * here. The handler forwards `code` + `state` to the API callback SERVER-SIDE
 * with the user's bearer (the bearer NEVER reaches the browser), then
 * 302-redirects the BROWSER to a FIXED same-origin path:
 * `/settings/integrations?connected={provider}` on success or `?error={code}`
 * on failure.
 *
 * Security posture:
 *  - The redirect target is FIXED and never attacker-controlled — the path is a
 *    constant and the only interpolated values are a vetted provider (allow-set)
 *    and a short generic error code (lib/integrations.settingsRedirectPath), so
 *    there is NO open-redirect surface even though `state`/`code` are
 *    attacker-influenced URL params.
 *  - The full state-HMAC + PKCE + account-binding validation (the CSRF defense)
 *    runs on the API BEFORE any token exchange; this route only carries the
 *    bearer across the web->api hop. We never log `code`/`state`.
 *  - The API's error detail is NOT leaked verbatim into the URL — a failure
 *    collapses to a generic `error` code so the API stays the only oracle.
 *
 * The same-origin redirect base is derived from the forwarded `host` header
 * (Caddy in prod, the dev server locally) rather than `req.url` (sameOriginUrl
 * below): the dev server normalizes `req.url`'s host to `localhost`, which would
 * land the redirect on a DIFFERENT origin than the one carrying the
 * SameSite=Lax session cookie (`127.0.0.1` vs `localhost` are distinct cookie
 * origins) and silently bounce the user to /login. Building the redirect from
 * the real host keeps every hop on one origin so the session cookie rides
 * through. The host header is only ever used to construct a SAME-ORIGIN redirect
 * to a FIXED path — never reflected into a body — so it is not an injection or
 * open-redirect vector.
 */
function sameOriginUrl(req: Request, path: string): URL {
  const forwardedProto = req.headers.get("x-forwarded-proto");
  const host = req.headers.get("x-forwarded-host") ?? req.headers.get("host");
  if (host) {
    const url = new URL(req.url);
    const proto = forwardedProto ?? url.protocol.replace(":", "");
    return new URL(path, `${proto}://${host}`);
  }
  return new URL(path, req.url);
}

export async function GET(
  req: Request,
  ctx: { params: Promise<{ provider: string }> },
): Promise<Response> {
  const { provider } = await ctx.params;
  if (!isAllowedProvider(provider)) {
    // An unknown provider in the redirect URI is treated as a generic failure —
    // bounce to the settings page rather than leaking a distinct status.
    return NextResponse.redirect(
      sameOriginUrl(req, settingsRedirectPath({ error: "unknown_provider" })),
    );
  }

  const token = await getSessionToken();
  if (!token) {
    // No session on a top-level redirect means the cookie expired mid-flow —
    // send the user to log in, then back to settings.
    return NextResponse.redirect(sameOriginUrl(req, "/login?next=/settings/integrations"));
  }

  const incoming = new URL(req.url);
  const code = incoming.searchParams.get("code");
  const state = incoming.searchParams.get("state");
  // The provider sends `error=access_denied` (etc.) when the user declines
  // consent — short-circuit to a generic error without touching the API.
  if (!code || !state) {
    return NextResponse.redirect(sameOriginUrl(req, settingsRedirectPath({ error: "denied" })));
  }

  // Forward code+state to the API callback server-side. The API runs the full
  // state/PKCE/account-binding validation before any token exchange. We build
  // the upstream URL with URLSearchParams so the values are encoded exactly once
  // and never string-interpolated into the path.
  const upstream = new URL(`${apiBaseUrl()}/integrations/${encodeURIComponent(provider)}/callback`);
  upstream.searchParams.set("code", code);
  upstream.searchParams.set("state", state);

  let res: Response;
  try {
    res = await fetch(upstream, {
      headers: { authorization: `Bearer ${token}` },
      cache: "no-store",
    });
  } catch {
    return NextResponse.redirect(
      sameOriginUrl(req, settingsRedirectPath({ error: "unreachable" })),
    );
  }

  if (!res.ok) {
    // Any validation/exchange failure on the API collapses to a single generic
    // code — the API detail is never surfaced in the URL (no oracle).
    return NextResponse.redirect(sameOriginUrl(req, settingsRedirectPath({ error: "failed" })));
  }

  return NextResponse.redirect(sameOriginUrl(req, settingsRedirectPath({ connected: provider })));
}
