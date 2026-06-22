/**
 * Pure helpers for the integrations proxy routes (app/api/integrations/**) and
 * the /settings/integrations page. No DOM, no `server-only` imports — safe to
 * import from the edge and from Vitest (unit-tested in test/integrations.test.ts).
 *
 * The OAuth provider allow-set is the single gate that keeps the `{provider}`
 * path param from ever reaching the API as an arbitrary string. The API has its
 * own allow-set (the no-500-on-unknown-provider contract); this layer rejects
 * an unknown provider with a 404 BEFORE the cookie-derived bearer is attached,
 * so a probe for `/api/integrations/evil/authorize` never even hits the API.
 *
 * `microsoft` is reserved for Phase 46 (config-only there) — kept out of the
 * allow-set now so an unconfigured provider 404s rather than half-working.
 */

/** The OAuth providers the web surface forwards. Phase 46 adds `microsoft`. */
export const ALLOWED_PROVIDERS = ["google"] as const;

export type OAuthProvider = (typeof ALLOWED_PROVIDERS)[number];

/** True iff `value` is a provider the web surface is allowed to proxy. */
export function isAllowedProvider(value: string): value is OAuthProvider {
  return (ALLOWED_PROVIDERS as readonly string[]).includes(value);
}

/**
 * The FIXED, same-origin redirect target the public callback route bounces the
 * browser to once the API callback resolves. It is NEVER attacker-controlled —
 * the path is a constant and the only interpolated values are a vetted provider
 * (allow-set above) and a short, fixed error code — so there is no open-redirect
 * surface. `connected` carries the provider on success; `error` a generic code
 * on failure (the API detail is never leaked verbatim into the URL).
 */
export function settingsRedirectPath(
  result: { connected: OAuthProvider } | { error: string },
): string {
  const params = new URLSearchParams(
    "connected" in result ? { connected: result.connected } : { error: result.error },
  );
  return `/settings/integrations?${params.toString()}`;
}
