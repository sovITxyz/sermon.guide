import "server-only";

import { cookies } from "next/headers";
import { apiBaseUrl } from "./config";
import { SESSION_COOKIE } from "./session";
import type { DocumentListItem, DocumentListResponse, LibraryBook, LibraryResponse } from "./types";

/** Read the JWT out of the HttpOnly session cookie (server context only). */
export async function getSessionToken(): Promise<string | null> {
  const store = await cookies();
  return store.get(SESSION_COOKIE)?.value ?? null;
}

/** Thrown when there is no/expired session so callers can redirect to /login. */
export class UnauthenticatedError extends Error {}

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
