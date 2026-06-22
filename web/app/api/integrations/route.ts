import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail } from "@/lib/http";
import type { IntegrationsResponse } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Proxy the OAuth connections list with the bearer from the HttpOnly cookie
 * (Phase 44). The browser never holds the JWT and never sees the API origin
 * (the Phase 15/16 proxy pattern). The response carries NO token material —
 * only `provider`, `provider_account_email`, `scopes`, and timestamps
 * (api/integrations.py). Per-user data -> `cache: "no-store"`.
 *
 * The page server component fetches this via lib/api-server:getIntegrations();
 * this same-origin proxy exists for any future client-side refetch and keeps
 * the wire surface uniform with the other /api/* proxies.
 */
export async function GET(): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const res = await fetch(`${apiBaseUrl()}/integrations`, {
    headers: { authorization: `Bearer ${token}` },
    cache: "no-store",
  });

  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Could not load your integrations.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as IntegrationsResponse;
  return NextResponse.json(data);
}
