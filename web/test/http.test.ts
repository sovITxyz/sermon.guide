import { describe, expect, it } from "vitest";
import { clientIpHeaders, errorDetail } from "../lib/http";

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
