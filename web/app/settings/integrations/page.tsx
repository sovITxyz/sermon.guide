import { IntegrationsPanel } from "@/components/IntegrationsPanel";
import { UnauthenticatedError, getIntegrations } from "@/lib/api-server";
import { isAllowedProvider } from "@/lib/integrations";
import type { IntegrationConnection } from "@/lib/types";
import { redirect } from "next/navigation";

/**
 * /settings/integrations (Phase 44). A SERVER component: it fetches the user's
 * OAuth connections server-side (lib/api-server:getIntegrations — bearer stays
 * on the server, never reaches the browser) and renders each provider with its
 * account email + connected date and a Connect/Disconnect button (the
 * interactive island, IntegrationsPanel).
 *
 * The public callback route bounces back here with `?connected={provider}` or
 * `?error={code}` — both are short, fixed, server-vetted tokens (NOT free
 * attacker text): `connected` is re-validated against the provider allow-set
 * and `error` is mapped to one of a closed set of friendly messages, so neither
 * can inject markup or an open redirect. They render as a small status banner.
 */

/** Map the fixed callback error codes to friendly copy. Unknown -> generic. */
const ERROR_MESSAGES: Record<string, string> = {
  denied: "The connection was cancelled.",
  failed: "We couldn't complete the connection. Please try again.",
  unreachable: "We couldn't reach the integration service. Please try again.",
  unknown_provider: "That integration isn't available.",
};

function errorMessage(code: string): string {
  return ERROR_MESSAGES[code] ?? "Something went wrong. Please try again.";
}

export default async function IntegrationsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  let connections: IntegrationConnection[];
  try {
    connections = await getIntegrations();
  } catch (err) {
    if (err instanceof UnauthenticatedError) {
      redirect("/login?next=/settings/integrations");
    }
    throw err;
  }

  const params = await searchParams;
  const connectedRaw = typeof params.connected === "string" ? params.connected : null;
  const errorRaw = typeof params.error === "string" ? params.error : null;
  // Re-validate `connected` against the allow-set so a hand-crafted
  // `?connected=<arbitrary>` cannot echo unvetted text into the banner.
  const connected = connectedRaw && isAllowedProvider(connectedRaw) ? connectedRaw : null;

  return (
    <section>
      <h1 className="mb-2 font-semibold text-xl">Integrations</h1>
      <p className="mb-6 text-gray-600 text-sm">
        Connect a Google account to push finished sermons into your Drive.
      </p>

      {connected ? (
        // <output> carries an implicit role="status" (a polite live region)
        // without colliding with Next's always-present route announcer's
        // role="alert" — same posture as SermonList's undo toast (web/AGENTS.md).
        <output className="mb-4 block rounded border border-green-200 bg-green-50 p-3 text-green-800 text-sm">
          Connected to {connected === "google" ? "Google Drive" : connected}.
        </output>
      ) : null}
      {errorRaw ? (
        <p
          role="alert"
          className="mb-4 rounded border border-red-200 bg-red-50 p-3 text-red-700 text-sm"
        >
          {errorMessage(errorRaw)}
        </p>
      ) : null}

      <IntegrationsPanel connections={connections} />
    </section>
  );
}
