/**
 * Forward the real client IP to the API for per-IP rate limiting (Phase 19).
 *
 * In prod the inbound X-Forwarded-For is trustworthy end-to-end: Caddy is the
 * only host-published service and it DISCARDS any client-supplied XFF (no
 * trusted_proxies configured), writing the true TCP peer instead — so the
 * value this handler receives is Caddy-attested, and forwarding it verbatim
 * preserves that attestation. The API only honors the header when
 * SERMON_API_TRUST_PROXY_HEADERS=true (set by the prod compose); in dev the
 * API ignores it and keys on the TCP peer, so forwarding unconditionally here
 * is safe everywhere.
 */
export function clientIpHeaders(req: Request): Record<string, string> {
  const xff = req.headers.get("x-forwarded-for");
  return xff ? { "x-forwarded-for": xff } : {};
}

/**
 * Extract a human-readable message from a FastAPI error response without
 * leaking internals to the browser. FastAPI returns `{detail: string}` for
 * handled HTTPExceptions and `{detail: [...]}` for 422 validation errors; we
 * surface the string form and fall back to a generic message otherwise.
 */
export async function errorDetail(res: Response, fallback: string): Promise<string> {
  try {
    const data = (await res.json()) as { detail?: unknown };
    if (typeof data.detail === "string") {
      return data.detail;
    }
  } catch {
    // Non-JSON or empty body — fall through to the generic message.
  }
  return fallback;
}
