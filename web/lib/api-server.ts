import "server-only";

import { cookies } from "next/headers";
import { apiBaseUrl } from "./config";
import { SESSION_COOKIE } from "./session";
import type {
  DocumentFull,
  DocumentListItem,
  DocumentListResponse,
  LibraryBook,
  LibraryResponse,
} from "./types";

/** Read the JWT out of the HttpOnly session cookie (server context only). */
export async function getSessionToken(): Promise<string | null> {
  const store = await cookies();
  return store.get(SESSION_COOKIE)?.value ?? null;
}

/** Thrown when there is no/expired session so callers can redirect to /login. */
export class UnauthenticatedError extends Error {}

/**
 * Thrown when the API returns the uniform 404 for a document the caller cannot
 * see (non-owned, nonexistent, non-UUID, or soft-deleted — Phase 34's
 * no-existence-oracle posture). The editor shell renders a not-found state
 * rather than redirecting, so the difference between "no session" and
 * "not your sermon" never leaks as a different page.
 */
export class DocumentNotFoundError extends Error {}

/**
 * Fetch the authenticated user's library straight from the API, server-side,
 * attaching the bearer from the HttpOnly cookie. The token never reaches the
 * browser. Returns `[]` for an empty library; raises `UnauthenticatedError`
 * on a 401 so the server component can redirect to /login.
 */
export async function getLibrary(): Promise<LibraryBook[]> {
  const token = await getSessionToken();
  if (!token) {
    throw new UnauthenticatedError();
  }
  const res = await fetch(`${apiBaseUrl()}/library`, {
    headers: { authorization: `Bearer ${token}` },
    // Per-user data — never cache across requests.
    cache: "no-store",
  });
  if (res.status === 401) {
    throw new UnauthenticatedError();
  }
  if (!res.ok) {
    throw new Error(`Library fetch failed (${res.status}).`);
  }
  const data = (await res.json()) as LibraryResponse;
  return data.books;
}

/**
 * Fetch the authenticated user's sermons (preview-only list items — no full
 * `content`), server-side, attaching the bearer from the HttpOnly cookie. The
 * token never reaches the browser. Returns `[]` for no sermons; raises
 * `UnauthenticatedError` on a 401 so the /sermons server component can redirect
 * to /login. Mirrors getLibrary().
 */
export async function getDocuments(): Promise<DocumentListItem[]> {
  const token = await getSessionToken();
  if (!token) {
    throw new UnauthenticatedError();
  }
  const res = await fetch(`${apiBaseUrl()}/documents`, {
    headers: { authorization: `Bearer ${token}` },
    // Per-user data — never cache across requests.
    cache: "no-store",
  });
  if (res.status === 401) {
    throw new UnauthenticatedError();
  }
  if (!res.ok) {
    throw new Error(`Documents fetch failed (${res.status}).`);
  }
  const data = (await res.json()) as DocumentListResponse;
  return data.documents;
}

/**
 * Fetch one full sermon (incl. the `content` ProseMirror JSON and the
 * server-derived `content_text`/`updated_at`) for the editor server shell,
 * attaching the bearer from the HttpOnly cookie. The token never reaches the
 * browser. Raises `UnauthenticatedError` on a 401 (-> redirect to /login) and
 * `DocumentNotFoundError` on the API's uniform 404 so the shell can render a
 * not-found state without an existence oracle. Mirrors getLibrary().
 */
export async function getDocument(documentId: string): Promise<DocumentFull> {
  const token = await getSessionToken();
  if (!token) {
    throw new UnauthenticatedError();
  }
  const res = await fetch(`${apiBaseUrl()}/documents/${encodeURIComponent(documentId)}`, {
    headers: { authorization: `Bearer ${token}` },
    // Per-user data — never cache across requests.
    cache: "no-store",
  });
  if (res.status === 401) {
    throw new UnauthenticatedError();
  }
  if (res.status === 404) {
    throw new DocumentNotFoundError();
  }
  if (!res.ok) {
    throw new Error(`Document fetch failed (${res.status}).`);
  }
  return (await res.json()) as DocumentFull;
}
