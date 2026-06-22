import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail } from "@/lib/http";
import { isAllowedProvider } from "@/lib/integrations";
import type { AuthorizeResponse } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Kick off an OAuth connect (Phase 44). The Connect button on
 * /settings/integrations POSTs here; this same-origin proxy attaches the bearer
 * from the HttpOnly cookie and asks the API to mint the state HMAC + PKCE
 * challenge (the verifier is stored server-side in Redis, never in the browser)
 * and build the provider auth URL. We return `{authorize_url}` and the client
 * sets `window.location` to it — a TOP-LEVEL navigation, so the SameSite=Lax
 * session cookie survives the round-trip back to the public callback route.
 *
 * The `{provider}` path param is validated against the allow-set BEFORE the
 * bearer is attached — an unknown provider is a 404 here and never reaches the
 * API. NOTHING is read from the request body: the provider comes from the path
 * allow-set only, so a client cannot smuggle scopes/redirect_uri/etc. The
 * authorize URL, redirect_uri, and PKCE are all the API's to construct.
 *
 * Only the authenticated user's own connection can be initiated — the API
 * scopes the minted state to current_user.user_id (the account-binding CSRF
 * defense verified at the callback).
 */
async function authorize(provider: string): Promise<Response> {
  if (!isAllowedProvider(provider)) {
    return NextResponse.json({ error: "Unknown provider." }, { status: 404 });
  }

  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const res = await fetch(
    `${apiBaseUrl()}/integrations/${encodeURIComponent(provider)}/authorize`,
    {
      method: "POST",
      headers: { authorization: `Bearer ${token}` },
      cache: "no-store",
    },
  );

  if (!res.ok) {
    // 503 (OAuth/enc-key unconfigured) and any other error surface a generic
    // message — the API's 503 detail names only the missing env var, never a
    // value, and we never echo token material.
    return NextResponse.json(
      { error: await errorDetail(res, "Could not start the connection.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as AuthorizeResponse;
  return NextResponse.json(data);
}

export async function POST(
  _req: Request,
  ctx: { params: Promise<{ provider: string }> },
): Promise<Response> {
  const { provider } = await ctx.params;
  return authorize(provider);
}
