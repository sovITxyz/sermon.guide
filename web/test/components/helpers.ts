import { vi } from "vitest";

/**
 * A typed `fetch`-shaped stub. Component tests assert on the proxy contract,
 * not on jsdom's network, so `global.fetch` is replaced per-test. Keeping the
 * stub typed (no `any`) satisfies Biome's noExplicitAny.
 */
export type FetchStub = ReturnType<typeof vi.fn<typeof fetch>>;

/** Build a minimal `Response`-like object with a JSON body and `ok`/`status`. */
export function jsonResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  const ok = init?.ok ?? true;
  const status = init?.status ?? (ok ? 200 : 500);
  return {
    ok,
    status,
    json: () => Promise.resolve(body),
  } as Response;
}

/** A `Response` whose `.json()` rejects — exercises the `.catch(() => null)` path. */
export function brokenJsonResponse(init: { ok: boolean; status: number }): Response {
  return {
    ok: init.ok,
    status: init.status,
    json: () => Promise.reject(new Error("not json")),
  } as Response;
}

/** Install a typed `global.fetch` stub and return it for assertions. */
export function installFetch(impl: (input: RequestInfo | URL) => Promise<Response>): FetchStub {
  const stub = vi.fn<typeof fetch>(impl as typeof fetch);
  vi.stubGlobal("fetch", stub);
  return stub;
}
