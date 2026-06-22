"use client";

import type { AuthorizeResponse, IntegrationConnection } from "@/lib/types";
import { useRouter } from "next/navigation";
import { useCallback, useRef, useState } from "react";

/**
 * The /settings/integrations client island (Phase 44). The page server
 * component fetches the connection list (lib/api-server:getIntegrations, bearer
 * stays on the server) and passes it down; this island owns the interactive
 * Connect / Disconnect actions (a server component cannot mutate or set
 * `window.location`).
 *
 * Locked decisions:
 *  - **Connect** POSTs the same-origin `/api/integrations/{provider}/authorize`
 *    proxy, which returns `{authorize_url}`; the island then sets
 *    `window.location` to it — a TOP-LEVEL navigation so the SameSite=Lax
 *    session cookie survives the round-trip back to the public callback route.
 *    The browser never holds the bearer or the provider client id/secret.
 *  - **Disconnect** is confirm-gated (a token grant is not trivially
 *    re-established without re-consent) and DELETEs the same-origin
 *    `/api/integrations/{provider}` proxy. A 204 (revoked) and the uniform 404
 *    (already gone) both refresh the list; anything else surfaces a
 *    non-destructive inline error.
 *  - **Single mutation at a time** (`busyRef`) so a double-click never races.
 *  - No token material is ever fetched to or rendered in the browser — only
 *    `provider`, `provider_account_email`, `scopes`, and timestamps cross the
 *    wire (lib/types:IntegrationConnection).
 */

const PROVIDER_LABELS: Record<string, string> = {
  google: "Google Drive",
};

/** Pretty provider name for the UI, falling back to the raw id. */
function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider;
}

/** ISO-8601 -> YYYY-MM-DD. Deterministic (no locale/timezone). */
function formatDate(iso: string): string {
  return iso.slice(0, 10);
}

export function IntegrationsPanel({
  connections,
}: {
  connections: IntegrationConnection[];
}) {
  const router = useRouter();
  const [busyProvider, setBusyProvider] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A ref mirror of busyProvider so the async handlers gate on the freshest
  // value without re-creating the callbacks on every busy transition.
  const busyRef = useRef<string | null>(null);

  const onConnect = useCallback(async (): Promise<void> => {
    if (busyRef.current !== null) {
      return;
    }
    setError(null);
    busyRef.current = "google";
    setBusyProvider("google");
    try {
      const res = await fetch("/api/integrations/google/authorize", { method: "POST" });
      if (!res.ok) {
        const data = (await res.json().catch(() => null)) as { error?: string } | null;
        setError(data?.error ?? "Could not start the connection. Please try again.");
        busyRef.current = null;
        setBusyProvider(null);
        return;
      }
      const data = (await res.json()) as AuthorizeResponse;
      // Top-level navigation to the provider consent screen. The SameSite=Lax
      // session cookie survives so the public callback route can read it on the
      // way back. We do NOT clear the busy flag — the page is leaving.
      window.location.assign(data.authorize_url);
    } catch {
      setError("Network error. Please try again.");
      busyRef.current = null;
      setBusyProvider(null);
    }
  }, []);

  const onDisconnect = useCallback(
    async (connection: IntegrationConnection): Promise<void> => {
      if (busyRef.current !== null) {
        return;
      }
      const confirmed = window.confirm(
        `Disconnect ${providerLabel(connection.provider)} (${connection.provider_account_email})? You'll need to reconnect and re-consent to use it again.`,
      );
      if (!confirmed) {
        return;
      }
      setError(null);
      busyRef.current = connection.provider;
      setBusyProvider(connection.provider);
      try {
        const res = await fetch(`/api/integrations/${encodeURIComponent(connection.provider)}`, {
          method: "DELETE",
        });
        // 204 (revoked) and the uniform 404 (already gone) both mean "no longer
        // connected" -> refresh. Anything else is a real error.
        if (res.status === 204 || res.status === 404) {
          router.refresh();
        } else {
          setError("Could not disconnect. Please try again.");
        }
      } catch {
        setError("Network error. Please try again.");
      } finally {
        busyRef.current = null;
        setBusyProvider(null);
      }
    },
    [router],
  );

  const googleConnection = connections.find((c) => c.provider === "google") ?? null;

  return (
    <div>
      {error ? (
        <p role="alert" className="mb-3 text-red-600 text-sm">
          {error}
        </p>
      ) : null}

      <ul className="divide-y divide-gray-100">
        <li className="flex items-start justify-between gap-4 py-4">
          <div className="min-w-0">
            <p className="font-medium text-sm">{providerLabel("google")}</p>
            {googleConnection ? (
              <>
                <p className="mt-1 truncate text-gray-600 text-sm">
                  Connected as {googleConnection.provider_account_email}
                </p>
                <p className="mt-0.5 text-gray-400 text-xs">
                  Since {formatDate(googleConnection.connected_at)}
                </p>
              </>
            ) : (
              <p className="mt-1 text-gray-500 text-sm">
                Not connected. Connect to pull sermons into your Google Drive.
              </p>
            )}
          </div>
          {googleConnection ? (
            <button
              type="button"
              onClick={() => void onDisconnect(googleConnection)}
              disabled={busyProvider === "google"}
              aria-label="Disconnect Google Drive"
              className="shrink-0 rounded border border-gray-300 px-3 py-1.5 text-gray-600 text-sm hover:border-red-300 hover:text-red-600 disabled:opacity-50"
            >
              Disconnect
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void onConnect()}
              disabled={busyProvider === "google"}
              aria-label="Connect Google Drive"
              className="shrink-0 rounded bg-black px-3 py-1.5 font-medium text-sm text-white disabled:opacity-50"
            >
              {busyProvider === "google" ? "Connecting…" : "Connect"}
            </button>
          )}
        </li>
      </ul>
    </div>
  );
}
