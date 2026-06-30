import { getSessionToken } from "@/lib/api-server";
import { apiBaseUrl } from "@/lib/config";
import { errorDetail } from "@/lib/http";
import { searchQueryProblem, whitelistSummary } from "@/lib/summary";
import type { SummaryResponse } from "@/lib/types";
import { NextResponse } from "next/server";

/**
 * Proxy POST /search-summary with the bearer from the HttpOnly cookie. The body
 * goes through a STRUCTURAL whitelist (lib/summary.ts:whitelistSummary): `query`
 * plus the optional Phase 49 scope (`book_ids`/`collection_ids`) are forwarded,
 * `limit_chunks`/`rerank`/a smuggled `user_id` are dropped, so a client cannot
 * widen the retrieval fan-out through this proxy. The scope is an INTERSECTION —
 * the API clamps it to the JWT user's library — and is omitted (= whole library)
 * when absent.
 *
 * Unlike the other proxies this one holds a long-running upstream request:
 * the warm E2E is ~134s on the dev box (CPU rerank + LLM round-trip; Phase 14b
 * measured up to ~235s when every chunk prunes), so the upstream fetch gets an
 * explicit timeout well above that instead of hanging the handler forever on
 * a wedged upstream.
 */
const UPSTREAM_TIMEOUT_MS = 300_000;

export async function POST(req: Request): Promise<Response> {
  const token = await getSessionToken();
  if (!token) {
    return NextResponse.json({ error: "Not authenticated." }, { status: 401 });
  }

  const parsed = (await req.json().catch(() => null)) as unknown;
  const result = whitelistSummary(parsed);
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: 400 });
  }
  // Trim + apply the friendly empty-query guard; the API still owns the length
  // 422. A whitespace-only query trims to "" and surfaces the same copy as before.
  const query = result.body.query.trim();
  const problem = searchQueryProblem(query);
  if (problem) {
    return NextResponse.json({ error: problem }, { status: 400 });
  }

  let res: Response;
  try {
    res = await fetch(`${apiBaseUrl()}/search-summary`, {
      method: "POST",
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
      body: JSON.stringify({ ...result.body, query }),
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
  } catch (err) {
    const timedOut = err instanceof DOMException && err.name === "TimeoutError";
    return NextResponse.json(
      {
        error: timedOut
          ? "The summary took too long to generate. Please try again."
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

  const data = (await res.json()) as SummaryResponse;
  return NextResponse.json(data);
}
