/**
 * Forward the real client IP to the API for per-IP rate limiting (Phase 19).
 *
 * In prod the inbound X-Forwarded-For comes from Caddy, the only
 * host-published service. Modern Caddy (>=2.5, no trusted_proxies) REPLACES a
 * client-supplied XFF with the true TCP peer; even if a hop ever APPENDED
 * instead, the rightmost entry is still proxy-written. This handler forwards
 * the header verbatim and adds no hop of its own, and the API keys on the
 * RIGHTMOST entry (api/ratelimit.py:client_ip) — unforgeable under both
 * proxy behaviors. The API only honors the header when
 * SERMON_API_TRUST_PROXY_HEADERS=true (set by the prod compose); in dev the
 * API ignores it and keys on the TCP peer, so forwarding unconditionally here
 * is safe everywhere.
 */
export function clientIpHeaders(req: Request): Record<string, string> {
  const xff = req.headers.get("x-forwarded-for");
  return xff ? { "x-forwarded-for": xff } : {};
}

/**
 * Re-emit an upstream response byte-for-byte: status + body bytes +
 * content-type, nothing else. The reader proxies use this for the uniform
 * 404 — api/reader.py collapses non-UUID, nonexistent, and non-owned book
 * ids into one identical `{"detail": "Book not found."}` body (the
 * no-existence-oracle contract), and passing it through verbatim keeps that
 * parity a property of the API alone instead of one this layer re-derives.
 */
export async function passthroughResponse(res: Response): Promise<Response> {
  const headers: Record<string, string> = {};
  const contentType = res.headers.get("content-type");
  if (contentType) {
    headers["content-type"] = contentType;
  }
  return new Response(await res.arrayBuffer(), { status: res.status, headers });
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
