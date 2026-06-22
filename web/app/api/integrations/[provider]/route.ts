import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail, passthroughResponse } from "@/lib/http";
import { isAllowedProvider } from "@/lib/integrations";
import { NextResponse } from "next/server";

/**
 * Disconnect (revoke) one OAuth connection with the bearer from the HttpOnly
 * cookie (Phase 44). The `{provider}` path param is validated against the
 * allow-set BEFORE the bearer is attached — an unknown provider is a 404 here
 * and never reaches the API, so a probe for `/api/integrations/evil` cannot
 * even drive an upstream call.
 *
 * DELETE /integrations/{provider} -> the API deletes the (user_id, provider)
 * row scoped to the JWT user and best-effort revokes the token upstream; 204 on
 * success. A provider not connected for this user (or cross-tenant) is the
 * API's uniform 404 — passed through byte-for-byte so the no-existence-oracle
 * contract stays a property of the API alone. No token/ciphertext is ever in
 * any request or response body.
 */
export async function DELETE(
  _req: Request,
  ctx: { params: Promise<{ provider: string }> },
): Promise<Response> {
  const { provider } = await ctx.params;
  if (!isAllowedProvider(provider)) {
    return NextResponse.json({ error: "Unknown provider." }, { status: 404 });
  }

  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const res = await fetch(`${apiBaseUrl()}/integrations/${encodeURIComponent(provider)}`, {
    method: "DELETE",
    headers: { authorization: `Bearer ${token}` },
    cache: "no-store",
  });

  // The uniform 404 carries the API's canonical `{detail}` JSON — pass it
  // through byte-for-byte so the no-oracle contract is the API's alone.
  if (res.status === 404) {
    return passthroughResponse(res);
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not disconnect the integration.") },
      { status: res.status },
    );
  }

  // 204 No Content — no body to forward.
  return new Response(null, { status: 204 });
}
