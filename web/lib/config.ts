import "server-only";

/**
 * The single place the backend base URL is read. `server-only` makes importing
 * this module from a Client Component a build-time error, so the API origin —
 * and the bearer token threaded through it in api-server.ts — can never leak
 * into client JS. There is intentionally no `NEXT_PUBLIC_` variant.
 */
export function apiBaseUrl(): string {
  return process.env.API_BASE_URL ?? "http://localhost:8000";
}
