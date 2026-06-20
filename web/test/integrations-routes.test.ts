import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Route-handler tests for the OAuth integrations proxies (Phase 44). They drive
 * the App Router handlers directly with mocked cookie + fetch, asserting the
 * load-bearing security properties:
 *   - the {provider} path param is validated against the allow-set BEFORE any
 *     upstream call (an unknown provider 404s and never reaches the API);
 *   - the cookie-derived bearer is attached server-side and NEVER returned to
 *     the browser (no token/state/code in any response body);
 *   - the public callback redirects the browser to a FIXED same-origin path
 *     (?connected on success, ?error on failure) — no open redirect, and the
 *     API's error detail is never echoed verbatim.
 *
 * `@/lib/api-server` (getSessionToken) and `@/lib/config` (apiBaseUrl, which is
 * `server-only`) are mocked so the handlers run in the node test env.
 */

const getSessionToken = vi.fn<() => Promise<string | null>>();

vi.mock("@/lib/api-server", () => ({ getSessionToken }));
vi.mock("@/lib/config", () => ({ apiBaseUrl: () => "http://api.test" }));

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  getSessionToken.mockReset();
  getSessionToken.mockResolvedValue("cookie-jwt");
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** The single upstream call's [url, init] for bearer/encoding assertions. */
function lastFetchCall(): [string, RequestInit] {
  const call = fetchMock.mock.calls.at(-1);
  if (!call) {
    throw new Error("fetch was not called");
  }
  const [url, init] = call;
  return [String(url), (init ?? {}) as RequestInit];
}

function bearerOf(init: RequestInit): string | undefined {
  const headers = (init.headers ?? {}) as Record<string, string>;
  return headers.authorization;
}

describe("GET /api/integrations (list proxy)", () => {
  it("attaches the cookie bearer and returns the connections, never a token", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          connections: [
            {
              provider: "google",
              provider_account_email: "pastor@example.com",
              scopes: "openid email",
              connected_at: "2026-06-20T00:00:00Z",
              token_expiry: null,
            },
          ],
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    const { GET } = await import("@/app/api/integrations/route");

    const res = await GET();
    expect(res.status).toBe(200);
    const [url, init] = lastFetchCall();
    expect(url).toBe("http://api.test/integrations");
    expect(bearerOf(init)).toBe("Bearer cookie-jwt");

    const body = await res.text();
    expect(body).toContain("pastor@example.com");
    // Sanity: no token/ciphertext field shape ever appears.
    expect(body).not.toMatch(/refresh_token|access_token|ciphertext/);
  });

  it("401s without a session and never calls upstream", async () => {
    getSessionToken.mockResolvedValue(null);
    const { GET } = await import("@/app/api/integrations/route");
    const res = await GET();
    expect(res.status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("POST /api/integrations/[provider]/authorize (kickoff proxy)", () => {
  it("rejects an unknown provider with 404 before any upstream call", async () => {
    const { POST } = await import("@/app/api/integrations/[provider]/authorize/route");
    const res = await POST(new Request("http://web.test/x", { method: "POST" }), {
      params: Promise.resolve({ provider: "evil" }),
    });
    expect(res.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("forwards the bearer and returns the authorize_url for an allowed provider", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({ authorize_url: "https://accounts.google.com/o/oauth2/v2/auth?x=1" }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      ),
    );
    const { POST } = await import("@/app/api/integrations/[provider]/authorize/route");
    const res = await POST(new Request("http://web.test/x", { method: "POST" }), {
      params: Promise.resolve({ provider: "google" }),
    });
    expect(res.status).toBe(200);
    const [url, init] = lastFetchCall();
    expect(url).toBe("http://api.test/integrations/google/authorize");
    expect(init.method).toBe("POST");
    expect(bearerOf(init)).toBe("Bearer cookie-jwt");
    const data = (await res.json()) as { authorize_url: string };
    expect(data.authorize_url).toContain("accounts.google.com");
  });

  it("surfaces a generic 503 when the API reports OAuth unconfigured", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "SERMON_API_GOOGLE_CLIENT_ID is not set." }), {
        status: 503,
        headers: { "content-type": "application/json" },
      }),
    );
    const { POST } = await import("@/app/api/integrations/[provider]/authorize/route");
    const res = await POST(new Request("http://web.test/x", { method: "POST" }), {
      params: Promise.resolve({ provider: "google" }),
    });
    expect(res.status).toBe(503);
  });
});

describe("DELETE /api/integrations/[provider] (revoke proxy)", () => {
  it("rejects an unknown provider with 404 before any upstream call", async () => {
    const { DELETE } = await import("@/app/api/integrations/[provider]/route");
    const res = await DELETE(new Request("http://web.test/x", { method: "DELETE" }), {
      params: Promise.resolve({ provider: "evil" }),
    });
    expect(res.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("forwards the bearer and returns 204 on success", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    const { DELETE } = await import("@/app/api/integrations/[provider]/route");
    const res = await DELETE(new Request("http://web.test/x", { method: "DELETE" }), {
      params: Promise.resolve({ provider: "google" }),
    });
    expect(res.status).toBe(204);
    const [url, init] = lastFetchCall();
    expect(url).toBe("http://api.test/integrations/google");
    expect(init.method).toBe("DELETE");
    expect(bearerOf(init)).toBe("Bearer cookie-jwt");
  });

  it("passes the uniform 404 through byte-for-byte (no oracle)", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "Integration not found." }), {
        status: 404,
        headers: { "content-type": "application/json" },
      }),
    );
    const { DELETE } = await import("@/app/api/integrations/[provider]/route");
    const res = await DELETE(new Request("http://web.test/x", { method: "DELETE" }), {
      params: Promise.resolve({ provider: "google" }),
    });
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ detail: "Integration not found." });
  });
});

describe("GET /api/integrations/[provider]/callback (public redirect URI)", () => {
  async function callback(provider: string, query: string) {
    const { GET } = await import("@/app/api/integrations/[provider]/callback/route");
    return GET(new Request(`http://web.test/api/integrations/${provider}/callback${query}`), {
      params: Promise.resolve({ provider }),
    });
  }

  it("forwards code+state with the bearer and 302s to the FIXED success path", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ provider: "google", provider_account_email: "a@b.c" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const res = await callback("google", "?code=abc123&state=signed.state");
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toBe(
      "http://web.test/settings/integrations?connected=google",
    );

    const [url, init] = lastFetchCall();
    const upstream = new URL(url);
    expect(upstream.origin + upstream.pathname).toBe(
      "http://api.test/integrations/google/callback",
    );
    expect(upstream.searchParams.get("code")).toBe("abc123");
    expect(upstream.searchParams.get("state")).toBe("signed.state");
    expect(bearerOf(init)).toBe("Bearer cookie-jwt");

    // The bearer/code/state never reach the browser — only a Location header.
    expect(await res.text()).not.toContain("cookie-jwt");
    expect(res.headers.get("location")).not.toContain("abc123");
  });

  it("rejects an unknown provider by bouncing to a generic error (no upstream)", async () => {
    const res = await callback("evil", "?code=abc&state=s");
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toBe(
      "http://web.test/settings/integrations?error=unknown_provider",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("bounces to /login when the session cookie is gone", async () => {
    getSessionToken.mockResolvedValue(null);
    const res = await callback("google", "?code=abc&state=s");
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toBe("http://web.test/login?next=/settings/integrations");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("maps a consent denial (no code) to a generic error without calling upstream", async () => {
    const res = await callback("google", "?error=access_denied");
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toBe("http://web.test/settings/integrations?error=denied");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("collapses an API validation/exchange failure to a generic error (no detail leak)", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "invalid state HMAC for user 42" }), {
        status: 400,
        headers: { "content-type": "application/json" },
      }),
    );
    const res = await callback("google", "?code=abc&state=tampered");
    expect(res.status).toBe(307);
    const location = res.headers.get("location") ?? "";
    expect(location).toBe("http://web.test/settings/integrations?error=failed");
    // The API's detail string is never echoed into the redirect URL.
    expect(location).not.toContain("HMAC");
    expect(location).not.toContain("user 42");
  });
});
