import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Route-handler tests for the editor-link proxies (Phase 45). They drive the App
 * Router handlers directly with mocked cookie + fetch, asserting the load-bearing
 * security properties:
 *   - the cookie-derived bearer is attached server-side and NEVER returned to the
 *     browser (no token in any response body);
 *   - the document_id path segment is URL-encoded into the FIXED upstream path —
 *     the client never assembles an attacker-controlled URL, and no
 *     client-supplied provider_file_id is ever forwarded as authoritative;
 *   - the unlink body is whitelisted to EXACTLY {mode} (closed value set) — a
 *     smuggled field never reaches the API, and an out-of-set mode 400s here;
 *   - the API's uniform 404 (no oracle) and the 409 (already linked) pass through
 *     byte-for-byte.
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

const DOC_ID = "doc-1";
const params = Promise.resolve({ documentId: DOC_ID });

describe("POST /api/documents/[id]/editor-link (link proxy)", () => {
  it("forwards the bearer to the FIXED upstream path with no body and returns the status", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          state: "linked",
          web_url: "https://docs.google.com/document/d/abc/edit",
          remote_changed: false,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    const { POST } = await import("@/app/api/documents/[documentId]/editor-link/route");
    const res = await POST(new Request("http://web.test/x", { method: "POST" }), { params });

    expect(res.status).toBe(200);
    const [url, init] = lastFetchCall();
    expect(url).toBe("http://api.test/documents/doc-1/editor-link");
    expect(init.method).toBe("POST");
    expect(bearerOf(init)).toBe("Bearer cookie-jwt");
    // No request body is forwarded — the API owns the file id + provider.
    expect(init.body).toBeUndefined();

    const body = await res.text();
    expect(body).toContain("docs.google.com");
    // Never a token in the response.
    expect(body).not.toContain("cookie-jwt");
    expect(body).not.toMatch(/access_token|refresh_token|ciphertext/);
  });

  it("401s without a session and never calls upstream", async () => {
    getSessionToken.mockResolvedValue(null);
    const { POST } = await import("@/app/api/documents/[documentId]/editor-link/route");
    const res = await POST(new Request("http://web.test/x", { method: "POST" }), { params });
    expect(res.status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("passes the 409 already-linked through byte-for-byte", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({ detail: "Document is already linked to an external editor." }),
        {
          status: 409,
          headers: { "content-type": "application/json" },
        },
      ),
    );
    const { POST } = await import("@/app/api/documents/[documentId]/editor-link/route");
    const res = await POST(new Request("http://web.test/x", { method: "POST" }), { params });
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({
      detail: "Document is already linked to an external editor.",
    });
  });

  it("passes the uniform 404 through byte-for-byte (no oracle)", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "Document not found." }), {
        status: 404,
        headers: { "content-type": "application/json" },
      }),
    );
    const { POST } = await import("@/app/api/documents/[documentId]/editor-link/route");
    const res = await POST(new Request("http://web.test/x", { method: "POST" }), { params });
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ detail: "Document not found." });
  });
});

describe("GET /api/documents/[id]/editor-link/status (status proxy)", () => {
  it("forwards the bearer and returns {state, web_url, remote_changed}", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          state: "linked",
          web_url: "https://docs.google.com/document/d/abc/edit",
          remote_changed: true,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    const { GET } = await import("@/app/api/documents/[documentId]/editor-link/status/route");
    const res = await GET(new Request("http://web.test/x"), { params });
    expect(res.status).toBe(200);
    const [url, init] = lastFetchCall();
    expect(url).toBe("http://api.test/documents/doc-1/editor-link/status");
    expect(bearerOf(init)).toBe("Bearer cookie-jwt");
    const data = (await res.json()) as { remote_changed: boolean };
    expect(data.remote_changed).toBe(true);
  });

  it("passes the uniform 404 through byte-for-byte", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "Document not found." }), {
        status: 404,
        headers: { "content-type": "application/json" },
      }),
    );
    const { GET } = await import("@/app/api/documents/[documentId]/editor-link/status/route");
    const res = await GET(new Request("http://web.test/x"), { params });
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ detail: "Document not found." });
  });
});

describe("POST /api/documents/[id]/editor-link/pull (pull proxy)", () => {
  it("forwards the bearer and returns the full document", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          document_id: DOC_ID,
          title: "Pulled",
          content: { type: "doc", content: [] },
          content_text: "",
          schema_version: 1,
          created_at: "2026-06-22T00:00:00Z",
          updated_at: "2026-06-22T01:00:00Z",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    const { POST } = await import("@/app/api/documents/[documentId]/editor-link/pull/route");
    const res = await POST(new Request("http://web.test/x", { method: "POST" }), { params });
    expect(res.status).toBe(200);
    const [url, init] = lastFetchCall();
    expect(url).toBe("http://api.test/documents/doc-1/editor-link/pull");
    expect(init.method).toBe("POST");
    expect(bearerOf(init)).toBe("Bearer cookie-jwt");
    const data = (await res.json()) as { title: string };
    expect(data.title).toBe("Pulled");
  });

  it("passes the 413 (export too large) through byte-for-byte", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "Document content is too large." }), {
        status: 413,
        headers: { "content-type": "application/json" },
      }),
    );
    const { POST } = await import("@/app/api/documents/[documentId]/editor-link/pull/route");
    const res = await POST(new Request("http://web.test/x", { method: "POST" }), { params });
    expect(res.status).toBe(413);
  });
});

describe("POST /api/documents/[id]/editor-link/unlink (unlink proxy)", () => {
  it("whitelists the body to exactly {mode} and forwards the bearer", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ state: "unlinked", web_url: null, remote_changed: false }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const { POST } = await import("@/app/api/documents/[documentId]/editor-link/unlink/route");
    const req = new Request("http://web.test/x", {
      method: "POST",
      headers: { "content-type": "application/json" },
      // A smuggled provider_file_id alongside a valid mode.
      body: JSON.stringify({ mode: "keep-app", provider_file_id: "attacker-file" }),
    });
    const res = await POST(req, { params });
    expect(res.status).toBe(200);

    const [url, init] = lastFetchCall();
    expect(url).toBe("http://api.test/documents/doc-1/editor-link/unlink");
    expect(init.method).toBe("POST");
    expect(bearerOf(init)).toBe("Bearer cookie-jwt");
    // ONLY mode survives — the smuggled file id never reached the upstream body.
    expect(JSON.parse(String(init.body))).toEqual({ mode: "keep-app" });
    expect(String(init.body)).not.toContain("attacker-file");
  });

  it("400s an out-of-set mode without calling upstream", async () => {
    const { POST } = await import("@/app/api/documents/[documentId]/editor-link/unlink/route");
    const req = new Request("http://web.test/x", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ mode: "delete-everything" }),
    });
    const res = await POST(req, { params });
    expect(res.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("401s without a session and never calls upstream", async () => {
    getSessionToken.mockResolvedValue(null);
    const { POST } = await import("@/app/api/documents/[documentId]/editor-link/unlink/route");
    const req = new Request("http://web.test/x", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ mode: "keep-app" }),
    });
    const res = await POST(req, { params });
    expect(res.status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
