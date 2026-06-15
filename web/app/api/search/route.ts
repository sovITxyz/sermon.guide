import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail } from "@/lib/http";
import { whitelistSearch } from "@/lib/search";
import type { SearchResponse } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Proxy POST /search with the bearer from the HttpOnly cookie — the in-editor
 * LibraryDrawer's plumbing (Phase 37). Returns the RAW hybrid hits (no LLM
 * round-trip; the generative summary lives on /search-summary), so unlike that
 * proxy this one needs no long upstream timeout — /search is fast retrieval.
 *
 * The body goes through a STRUCTURAL whitelist (lib/search.ts): only `query` is
 * re-serialized upstream. `limit`/`rerank` are dropped before the body reaches
 * the API, so a client cannot widen the retrieval fan-out or flip off the
 * rerank/highlight pipeline through this proxy; a smuggled `user_id`/`book_ids`
 * is dropped likewise (it would otherwise 422 on the API's `extra="forbid"`).
 *
 * Tenant isolation: POST /search resolves the `book_id` set server-side from the
 * JWT user's library (api/search.py run_search) — SearchRequest has NO
 * `book_ids`/`user_id`. This proxy adds NO query of its own and only forwards
 * the cookie-derived bearer, so it introduces no new tenant surface.
 */
const UPSTREAM_TIMEOUT_MS = 60_000;

export async function POST(req: Request): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const parsed = (await req.json().catch(() => null)) as unknown;
  const result = whitelistSearch(parsed);
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: 400 });
  }

  let res: Response;
  try {
    res = await fetch(`${apiBaseUrl()}/search`, {
      method: "POST",
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
      body: JSON.stringify(result.body),
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
  } catch (err) {
    const timedOut = err instanceof DOMException && err.name === "TimeoutError";
    return NextResponse.json(
      {
        error: timedOut
          ? "The search took too long. Please try again."
          : "Could not reach the search service.",
      },
      { status: timedOut ? 504 : 502 },
    );
  }

  if (!res.ok) {
    return NextResponse.json(
      { error: await errorDetail(res, "Search failed.") },
      { status: res.status },
    );
  }

  const data = (await res.json()) as SearchResponse;
  return NextResponse.json(data);
}
