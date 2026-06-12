import { describe, expect, it } from "vitest";
import { clientIpHeaders, errorDetail, passthroughResponse } from "../lib/http";

describe("clientIpHeaders", () => {
  it("forwards the inbound X-Forwarded-For verbatim (Caddy-attested in prod)", () => {
    const req = new Request("http://localhost/api/auth/login", {
      headers: { "x-forwarded-for": "203.0.113.9" },
    });
    expect(clientIpHeaders(req)).toEqual({ "x-forwarded-for": "203.0.113.9" });
  });

  it("sends nothing when the header is absent — never fabricates an address", () => {
    const req = new Request("http://localhost/api/auth/login");
    expect(clientIpHeaders(req)).toEqual({});
  });
});

describe("passthroughResponse", () => {
  it("re-emits status, body bytes, and content-type verbatim (uniform-404 parity)", async () => {
    const body = JSON.stringify({ detail: "Book not found." });
    const upstream = new Response(body, {
      status: 404,
      headers: { "content-type": "application/json" },
    });
    const out = await passthroughResponse(upstream);
    expect(out.status).toBe(404);
    expect(out.headers.get("content-type")).toBe("application/json");
    expect(await out.text()).toBe(body);
  });

  it("omits content-type rather than fabricating one when the upstream has none", async () => {
    const out = await passthroughResponse(new Response(null, { status: 404 }));
    expect(out.status).toBe(404);
    expect(out.headers.get("content-type")).toBeNull();
    expect(await out.text()).toBe("");
  });
});

describe("errorDetail", () => {
  it("surfaces FastAPI string details (e.g. the 429 limiter message) verbatim", async () => {
    const res = new Response(JSON.stringify({ detail: "Too many requests." }), {
      status: 429,
      headers: { "content-type": "application/json" },
    });
    expect(await errorDetail(res, "fallback")).toBe("Too many requests.");
  });

  it("falls back on non-string details and non-JSON bodies", async () => {
    const validation = new Response(JSON.stringify({ detail: [{ loc: ["body"] }] }), {
      status: 422,
    });
    expect(await errorDetail(validation, "fallback")).toBe("fallback");
    const text = new Response("rate limited", { status: 429 });
    expect(await errorDetail(text, "fallback")).toBe("fallback");
  });
});
