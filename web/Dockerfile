# web/ — Next.js 15 frontend. BUILD FROM web/:
#
#   docker build web/
#
# web/ is fully independent (no Python imports; talks to api/ over HTTP
# only), so its context is just this directory.
#
# Uses the `output: "standalone"` bundle from next.config.ts: the runner
# stage ships only .next/standalone + .next/static and starts with
# `node server.js` (NOT `next start`), keeping the image small.
#
# API_BASE_URL is a RUNTIME env var (read via server-only lib/config.ts,
# never inlined at build), so one image works against any api origin —
# do not pass it as a build ARG and do not invent a NEXT_PUBLIC_ variant
# (that would leak the API origin into client JS and break the auth model).

FROM node:20-slim AS builder
# pnpm 9 pinned explicitly: package.json deliberately has NO packageManager
# field (it conflicts with CI's pnpm/action-setup@v6 version pin — web/AGENTS.md).
RUN npm install -g pnpm@9
WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .
RUN pnpm build

FROM node:20-slim
# NODE_ENV=production is load-bearing: lib/session.ts gates the session
# cookie's Secure attribute on it. HOSTNAME=0.0.0.0 so the standalone
# server binds beyond the container loopback.
ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0
WORKDIR /app
# Run as the unprivileged `node` user baked into the base image (uid 1000).
# next.config.ts pins outputFileTracingRoot to web/, so the standalone bundle
# is flat: server.js sits at the root of .next/standalone. --chown keeps the
# copied tree owned by node so no root-owned files leak into the runtime.
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
USER node
EXPOSE 3000
CMD ["node", "server.js"]
