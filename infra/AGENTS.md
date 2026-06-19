# infra/ — agent instructions

Local-development AND v0 single-box production infrastructure for
sermon.guide, plus the provider-portable k8s/KEDA deploy direction in
`k8s/` (Phase 30). The `docker-compose` files stay the local-dev + single-box
paths; k8s is additive, not a replacement.

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
- `k8s/` — provider-portable Kubernetes manifests (Phase 30). **Raw manifests
  + kustomize**, NOT Helm: `base/` holds the workload shapes (api/web/worker
  Deployments on the Phase 29 GHCR images, Services, web Ingress, the KEDA
  `ScaledObject` + `TriggerAuthentication`, ConfigMap, secret template), and
  `overlays/prod/` patches image tags + replica counts + external store
  endpoints. Apply with `kubectl apply -k` (kustomize is built into kubectl).
  Data stores (Postgres/Redis/Milvus) are modelled as **external/managed**
  (endpoints from the ConfigMap, creds from the Secret); in-cluster
  StatefulSets are the documented self-hosted alternative. See
  [`k8s/README.md`](./k8s/README.md) for secret creation, the KEDA install
  command, and the live kind+KEDA scale test (operator/CI step).

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

### k8s manifests (`k8s/`)

- **Raw manifests + kustomize, never Helm.** Layout is `base/` (workload
  shapes) + `overlays/<env>/` (env patches). The prod overlay owns image-tag
  pinning, replica counts, and external-store endpoints; `base/` stays
  cluster-portable (EKS/GKE/AKS/kind).
- **No committed Secrets, ever.** `base/` ships only a
  `secret.example.yaml` template (placeholder values, real one gitignored);
  the real Secret is created out-of-band (`kubectl create secret` /
  sealed-secrets / SOPS, documented in `k8s/README.md`). Secret values never
  go in a ConfigMap and are never baked into an image. Non-secret
  hosts/ports/flags live in the `sermon-config` ConfigMap; all credentials
  live in the `sermon-secrets` Opaque Secret.
- **Pin images to the immutable `:sha-<commit>` tag in the prod overlay**, not
  `:latest`, for reproducible rollouts. The Phase 29 GHCR packages are
  **private by default** → every app Deployment needs the `regcred`
  docker-registry `imagePullSecret` or pods `ImagePullBackOff`.
- **KEDA trigger = Redis scaler, `listName: celery`, `databaseIndex: 0`**
  (the default Celery queue on the broker DB; matches `api/metrics.py`'s
  `_CELERY_QUEUE_KEY`). `minReplicaCount: 0` (the §2 scale-to-zero decision),
  and `cooldownPeriod` MUST be `>=` the 300s broker `visibility_timeout`
  (`celery_app.py`) with worker `terminationGracePeriodSeconds ~120` (compose
  parity) so a long `acks_late` ingest is not killed mid-flight.
- **`SERMON_API_LLM_PROVIDER=deepinfra` is mandatory in the ConfigMap.** The
  compose prod path defaults this to `google` (the known `/search-summary` 503
  bug when `GOOGLE_API_KEY` is unset) — the k8s ConfigMap hardcodes
  `deepinfra` so the bug cannot ride along.
- **`api` is ClusterIP-only, never an Ingress path browsers hit.** Only `web`
  is public (Service + Ingress, TLS via cert-manager — replacing compose's
  Caddy). `worker` has no Service. This preserves the compose invariant that
  browsers only reach the Next route handlers, which talk to `api` over the
  cluster network. `api` trusts XFF (`SERMON_API_TRUST_PROXY_HEADERS=true`)
  only because it has no public Ingress.
