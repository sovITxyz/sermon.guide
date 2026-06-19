import { fileURLToPath } from "node:url";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Self-contained server bundle at .next/standalone for the Docker image
  // (web/Dockerfile runs `node server.js`, not `next start`). Dev (`next dev`)
  // and CI (`tsc`/`biome`/`vitest`, no build) are unaffected.
  output: "standalone",
  // Pin the file-tracing root to this directory so the standalone bundle
  // always lands at .next/standalone/server.js. Without this, Next walks up
  // the tree looking for a lockfile and, when web/ is checked out inside the
  // monorepo (or any host with a stray parent lockfile), nests server.js under
  // the repo path — which would break the Dockerfile's flat `COPY .next/
  // standalone ./` + `node server.js`. In the hermetic image the context is
  // just web/, so this is a no-op there; on the host it makes builds match.
  outputFileTracingRoot: fileURLToPath(new URL(".", import.meta.url)),
};

export default nextConfig;
