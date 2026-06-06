import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Self-contained server bundle at .next/standalone for the Docker image
  // (web/Dockerfile runs `node server.js`, not `next start`). Dev (`next dev`)
  // and CI (`tsc`/`biome`/`vitest`, no build) are unaffected.
  output: "standalone",
};

export default nextConfig;
