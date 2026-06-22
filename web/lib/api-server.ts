import "server-only";

import { cookies } from "next/headers";
import { apiBaseUrl } from "./config";
import { SESSION_COOKIE } from "./session";
import type {
  DocumentFull,
  DocumentListItem,
  DocumentListResponse,
  EditorLinkStatus,
  IntegrationConnection,
  IntegrationsResponse,
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

/**
 * Fetch the authenticated user's OAuth connections (Phase 44), server-side,
 * attaching the bearer from the HttpOnly cookie. The token never reaches the
 * browser. The response carries NO token material — only `provider`,
 * `provider_account_email`, `scopes`, and timestamps (api/integrations.py).
 * Returns `[]` when nothing is connected; raises `UnauthenticatedError` on a
 * 401 so the /settings/integrations server component can redirect to /login.
 * Mirrors getDocuments().
 */
export async function getIntegrations(): Promise<IntegrationConnection[]> {
  const token = await getSessionToken();
  if (!token) {
    throw new UnauthenticatedError();
  }
  const res = await fetch(`${apiBaseUrl()}/integrations`, {
    headers: { authorization: `Bearer ${token}` },
    // Per-user data — never cache across requests.
    cache: "no-store",
  });
  if (res.status === 401) {
    throw new UnauthenticatedError();
  }
  if (!res.ok) {
    throw new Error(`Integrations fetch failed (${res.status}).`);
  }
  const data = (await res.json()) as IntegrationsResponse;
  return data.connections;
}

/**
 * Fetch the external-editor link status for one sermon (Phase 45), server-side
 * on doc open, attaching the bearer from the HttpOnly cookie. The token never
 * reaches the browser. This drives the editor's read-only lock: when the
 * returned `state` is `linked` the editor opens HARD read-only with the
 * "Editing externally" banner; otherwise it opens editable.
 *
 * The API returns the link status for the JWT user's OWN document only (the
 * owned-document gate is the API's; a non-owned / nonexistent / soft-deleted id
 * is the uniform no-oracle 404). When the document has NO active link the API
 * returns `state: "unlinked"` with a 200 — so a 404 here is the document-gate
 * 404, not a "no link" signal, and the page treats it as "render unlinked"
 * (the document fetch already established the doc exists for this user). Any
 * other failure is non-fatal: the editor opens unlinked (editable) rather than
 * blocking on a transient status error. NO token/file-id material is in the
 * payload — only `{state, web_url, remote_changed}` (lib/types.ts).
 */
export async function getEditorLinkStatus(documentId: string): Promise<EditorLinkStatus> {
  const unlinked: EditorLinkStatus = { state: "unlinked", web_url: null, remote_changed: false };
  const token = await getSessionToken();
  if (!token) {
    throw new UnauthenticatedError();
  }
  const res = await fetch(
    `${apiBaseUrl()}/documents/${encodeURIComponent(documentId)}/editor-link/status`,
    {
      headers: { authorization: `Bearer ${token}` },
      // Per-user data — never cache across requests.
      cache: "no-store",
    },
  );
  if (res.status === 401) {
    throw new UnauthenticatedError();
  }
  // No active link (or the no-oracle 404 / any transient failure) -> treat as
  // unlinked so the editor opens editable; the document fetch already proved
  // the doc is the user's, so this never hides a real not-found.
  if (!res.ok) {
    return unlinked;
  }
  return (await res.json()) as EditorLinkStatus;
}
