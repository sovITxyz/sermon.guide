# infra/ — agent instructions

Local-development AND v0 single-box production infrastructure for
sermon.guide. The k8s/KEDA shape stays post-v0
(see [docs/PHASES.md](../docs/PHASES.md), Beyond Phase 16).

## What lives here

- `docker-compose.yml` — local dev data plane: Postgres 16, Redis 7, Milvus
  standalone v2.6 with its required etcd + MinIO dependencies. Brought up via
  `make up` from repo root.
- `.env.example` — template for `infra/.env` (gitignored). `make up` copies
  the example to `.env` on first run.
- `docker-compose.prod.yml` — the v0 single-box AWS stack (data plane + api/
  worker/web + Caddy edge). DELIBERATELY self-contained, not an overlay:
  compose merges `ports:` additively, and "only Caddy publishes a port" is
  the security property the file guarantees. Keep its data-plane blocks in
  sync with `docker-compose.yml` when bumping versions. Runbook:
  [docs/DEPLOY_AWS.md](../docs/DEPLOY_AWS.md).
- `caddy/` — TLS edge (Dockerfile + Caddyfile: rate limits, body caps,
  default_sni for bare-IP deploys).
- `aws/` — provision/deploy/start/stop/status/destroy lifecycle scripts.
  Tag-based and re-runnable; secrets are generated ON the instance, never
  committed.
- `scripts/` — `backup.sh` / `restore.sh` (Phase 28), the bodies behind
  `make backup` / `make restore`. They source `infra/.env` for creds (never
  echo a value) and write to `BACKUP_DIR` (host, default `infra/backups/`,
  gitignored — NEVER a docker volume, which `make nuke` wipes). Runbook +
  restore drill: [docs/BACKUP_RESTORE.md](../docs/BACKUP_RESTORE.md).
- `backup/backup.yaml` — milvus-backup config (no secrets; MinIO creds are
  injected at runtime via `--set` from `infra/.env`).
- `backups/` — gitignored backup-artifact target (created on first
  `make backup`).
- `env.prod.template` — documents `/opt/sermon/.env.prod` (generated on-box
  by `aws/deploy.sh`). Deliberately NOT dot-env-named so repo tooling can
  read it.
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
