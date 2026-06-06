# Caddy with the rate-limit module — the single public-facing service.
#
# Stock caddy images don't ship rate limiting; mholt/caddy-ratelimit is the
# canonical module, compiled in via xcaddy. Rate limiting at the edge is the
# no-app-code mitigation for the Phase 19 gap (open signup + expensive
# /search-summary and /upload paths) until that phase lands properly.
FROM caddy:2.11.4-builder AS builder
RUN xcaddy build \
    --with github.com/mholt/caddy-ratelimit

FROM caddy:2.11.4
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
