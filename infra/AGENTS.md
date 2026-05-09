# infra/ — agent instructions

Local-development infrastructure for sermon.guide v0. Production lives in
k8s manifests later (see [docs/PHASES.md](../docs/PHASES.md), Beyond Phase 16).

## What lives here

- `docker-compose.yml` — Postgres 16, Redis 7, Milvus standalone v2.6 with its
  required etcd + MinIO dependencies. Brought up via `make up` from repo root.
- `.env.example` — template for `infra/.env` (gitignored). `make up` copies
  the example to `.env` on first run.
- Future: `k8s/` Helm values + KEDA scaler config (post-v0).

## Conventions

### Env-var naming: `SERMON_*`

Every variable in `.env` and the compose file uses the **`SERMON_`** prefix
(`SERMON_POSTGRES_PORT`, `SERMON_REDIS_PASSWORD`, ...). This avoids collisions
with the contributor's shell environment and makes `env | grep ^SERMON_` an
honest audit of what the stack needs.

Container-internal env vars (Postgres' own `POSTGRES_USER`, MinIO's
`MINIO_ROOT_USER`, Milvus' `MINIO_ACCESSKEYID`, ...) are mapped from the
`SERMON_*` ones inside `docker-compose.yml`. Don't sprinkle bare `POSTGRES_*`
into `.env` — keep the prefix.

### Healthchecks are mandatory

Every service defines a `healthcheck`. Milvus depends on etcd and MinIO with
`condition: service_healthy` so it waits for upstream readiness rather than
racing. `make up` runs `docker compose up -d --wait`, which blocks until
every service reports healthy or fails.

When adding a service, ship its `healthcheck` in the same change. No
"will add later".

### Compose schema

We use the modern **Compose Specification** (compose v2). The deprecated
top-level `version:` field is omitted — recent docker compose ignores it and
warns if present. Schema docs:
<https://docs.docker.com/compose/compose-file/>. The runtime is invoked as
`docker compose` (subcommand), never the legacy `docker-compose` binary.

### Adding a service

1. Add the service block to `docker-compose.yml` with: pinned image tag (no
   `latest`), named volume(s) for persistence, a working `healthcheck`, and
   `SERMON_*`-sourced ports/credentials.
2. Add the corresponding `SERMON_*` defaults to `.env.example`.
3. If anything depends on it, add `depends_on: { <svc>: { condition:
   service_healthy } }`.
4. Run `make down && make up` to confirm the stack is still idempotent.
