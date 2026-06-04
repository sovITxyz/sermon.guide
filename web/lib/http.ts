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
