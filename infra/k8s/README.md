# infra/k8s — provider-portable Kubernetes manifests (Phase 30)

Raw manifests + **kustomize** (no Helm). This is the additive,
provider-portable deploy direction for sermon.guide — it does **not** replace
the docker-compose stacks:

- `infra/docker-compose.yml` — local-dev data plane (unchanged).
- `infra/docker-compose.prod.yml` — single-box AWS deploy (unchanged).
- `infra/k8s/` — portable across EKS/GKE/AKS/kind, applied with `kubectl -k`
  or `kustomize build`. Built into kubectl (≥1.14), so operators need no
  extra tool.

## Why raw + kustomize (not Helm)

Small, fixed topology — 3 app Deployments + Services + 1 KEDA ScaledObject +
1 ConfigMap/Secret. Helm's templating/values indirection and release-management
surface buy nothing here. kustomize ships inside kubectl, so there is no
Tiller/registry/extra-tool dependency. Layout:

```
base/                  workload-shape manifests (env-agnostic)
overlays/prod/         image-tag pins (:sha digest), replica counts,
                       real external DB/Redis/Milvus endpoints, public host
```

## Topology

| Component | Kind        | Exposure                                            |
|-----------|-------------|-----------------------------------------------------|
| web       | Deployment  | ClusterIP Service + **Ingress** (TLS, public)       |
| api       | Deployment  | ClusterIP Service **only** — never public           |
| worker    | Deployment  | **no Service** — consumes the Redis queue; KEDA-scaled |

Browsers only ever hit `web`. `web` reaches `api` over the cluster network at
`http://api:8000` (`API_BASE_URL`), mirroring the compose invariant. The
api stays ClusterIP-only — a misconfigured Ingress exposing `:8000` would be a
tenant-isolation-adjacent leak (the api trusts the rightmost `X-Forwarded-For`
when `TRUST_PROXY_HEADERS=true`, safe only because clients cannot reach it
directly).

## Data stores — external/managed (default) vs in-cluster

The base manifests model Postgres / Redis / Milvus as **external/managed**
(endpoints in the ConfigMap, credentials in the Secret):

- **Recommended (default):** managed Postgres (RDS/CloudSQL), managed Redis
  (ElastiCache/MemoryStore), managed Milvus (Zilliz Cloud or milvus-operator).
  Gets HA + backups, keeps stateful storage off the operator. Adds vendor cost
  and means the manifests reference endpoints they do not own.
- **Self-hosted alternative:** in-cluster StatefulSets (Postgres + Redis + the
  etcd/MinIO/Milvus trio from compose). Self-contained, matches compose, but
  the operator owns PVC/StatefulSet lifecycle and the **Phase 28 backup/restore
  tooling hardcodes compose container names — it does NOT cover in-cluster
  StatefulSets**, so recoverability regresses. If self-hosting Milvus, add a
  **NetworkPolicy** — compose Milvus has **no auth**, so network isolation is
  the only access control.

The prod overlay's `configMapGenerator`/JSON patches set the real endpoints
(`SERMON_POSTGRES_HOST`, `SERMON_REDIS_HOST`, `SERMON_MILVUS_HOST`) and the
KEDA scaler `host`.

## Prerequisites

1. A cluster (EKS/GKE/AKS or local kind/minikube) and `kubectl` ≥ 1.14.
2. **KEDA installed** (for the worker autoscaler):
   ```
   kubectl apply --server-side -f \
     https://github.com/kedacore/keda/releases/download/v2.14.0/keda-2.14.0.yaml
   ```
3. An **Ingress controller** (e.g. ingress-nginx) and **cert-manager** with a
   `letsencrypt-prod` ClusterIssuer (TLS for the web Ingress — replaces the
   compose Caddy edge). Patch `ingressClassName` / issuer in the overlay to
   match your cluster.
4. The Phase 29 GHCR images published (`.github/workflows/images.yml`).
5. External Postgres/Redis/Milvus reachable from the cluster (or the
   in-cluster StatefulSet alternative above).

## Secrets (never committed)

`secret.example.yaml` is a **template** documenting the required keys — it is
NOT applied by the kustomization. Create the real `sermon-secrets` out-of-band:

```sh
kubectl -n sermon create secret generic sermon-secrets \
  --from-literal=SERMON_API_JWT_SECRET="$(openssl rand -hex 48)" \
  --from-literal=SERMON_POSTGRES_USER='...' \
  --from-literal=SERMON_POSTGRES_PASSWORD='...' \
  --from-literal=SERMON_POSTGRES_DB='...' \
  --from-literal=SERMON_REDIS_PASSWORD='...' \
  --from-literal=DEEPINFRA_API_KEY='...'
```

- `SERMON_API_JWT_SECRET` is **required** — the api boot guard refuses to start
  if it is unset/empty or equals the dev placeholder (the pod crash-loops).
- `DEEPINFRA_API_KEY` is **required** — every search/ingest 503s/fails without
  it (ADR 0006 remote inference).
- For GitOps, use **sealed-secrets** or **SOPS** instead of `kubectl create
  secret` so an encrypted form can be checked in. Never commit a plaintext
  Secret (`infra/k8s/**/secret.yaml` is `.gitignore`d).

### GHCR image pull secret (required)

The Phase 29 packages are **private by default** — without this the pods
`ImagePullBackOff`:

```sh
kubectl -n sermon create secret docker-registry regcred \
  --docker-server=ghcr.io \
  --docker-username='<github-user>' \
  --docker-password='<github-PAT-with-read:packages>'
```

Every app Deployment + bootstrap Job references `imagePullSecrets: [regcred]`.

## Apply order

```sh
# 1. Namespace + secrets (out-of-band; not in the kustomization)
kubectl create namespace sermon
#    ... create sermon-secrets and regcred as above ...

# 2. One-shot bootstrap BEFORE the first app rollout (mirrors compose `ops`):
kubectl -n sermon apply -f infra/k8s/base/migrate-job.yaml   # alembic + milvus
kubectl -n sermon wait --for=condition=complete job/migrate job/bootstrap-milvus --timeout=300s

# 3. The app + KEDA resources
kubectl apply -k infra/k8s/overlays/prod
```

Re-running the Jobs: delete then re-apply (`kubectl delete -f
infra/k8s/base/migrate-job.yaml` first). Both are idempotent (alembic at head;
bootstrap is create-if-absent).

## KEDA autoscaling

The `ScaledObject` scales the `worker` Deployment on Celery broker queue depth:
the `redis` scaler watches `listName: celery` on `databaseIndex: 0` (the
broker db — confirmed from the codebase: no custom `task_routes`/queues, so all
tasks land on Celery's default `celery` list; matches `api/metrics.py`).

- `minReplicaCount: 0` — scales workers to zero (§2 locked decision).
- `maxReplicaCount: 10` — honest cap for I/O-bound remote-inference ingest.
- `cooldownPeriod: 300` — **must be ≥ the 300s broker `visibility_timeout`**
  (`worker/celery_app.py`) so KEDA does not scale-to-zero out from under an
  `acks_late` in-flight ingest (which would be requeued). The worker's
  `terminationGracePeriodSeconds: 120` matches compose's `stop_grace_period`.
- `LLEN(celery)` is an **approximation** — it undercounts in-flight `acks_late`
  messages, so a single stuck task is invisible to it; tune `listLength`
  conservatively.

The Redis password reaches the scaler via the `TriggerAuthentication`
(`keda-redis-auth`), which pulls `SERMON_REDIS_PASSWORD` from `sermon-secrets`
— no secret value in the ScaledObject.

## Shared upload dir (read this before going multi-node)

api and worker share `SERMON_API_UPLOAD_DIR=/data/uploads` (the filesystem
upload handoff). In k8s a node-local `emptyDir`/`hostPath` is **not** visible
across pods on different nodes — with `minReplicaCount: 0` workers and api on
another node, a filesystem-only handoff **breaks ingest**. Before production:
mount a **ReadWriteMany PVC** (NFS/EFS/Filestore) at `/data/uploads` on both
Deployments, **or** move the upload handoff to object storage (R2/B2 per the
roadmap). This is intentionally left as an operator decision (provider-specific
storage class), not baked into the base.

## Validation

Tooling: kustomize v5.x (or kubectl ≥1.14) + kubeconform v0.6.x.

```sh
# Render (must succeed):
kustomize build infra/k8s/overlays/prod
kustomize build infra/k8s/base

# Schema-validate the render. KEDA CRDs need the kedacore schema location:
kustomize build infra/k8s/base | kubeconform -strict -summary \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/keda.sh/{{ .ResourceKind }}_{{ .ResourceAPIVersion }}.json'

# Structural client dry-run (needs a kubeconfig):
kubectl apply --dry-run=client -k infra/k8s/overlays/prod
```

## Live cluster test (operator / CI step)

The build environment for Phase 30 had **no cluster** (no kubectl/kustomize/
kind/minikube preinstalled — only docker). The render + kubeconform floor was
run; the live apply/scale test is the **operator/CI gate**, not claimed here.
Recipe (kind):

```sh
kind create cluster --name sermon
kubectl apply --server-side -f \
  https://github.com/kedacore/keda/releases/download/v2.14.0/keda-2.14.0.yaml
# create sermon-secrets + regcred, run the bootstrap Jobs, then:
kubectl apply -k infra/k8s/overlays/prod
```

Then prove the four Phase 30 acceptance criteria:

1. **Readiness gate holds a bad rollout.** Kill Postgres → `GET /readyz` 503s →
   api pods stay `NotReady` → the rollout is held (never serves).
2. **Scale-out on backlog.** Enqueue N ingest tasks (`LLEN celery` rises) →
   KEDA scales the worker Deployment up toward `maxReplicaCount`.
3. **Scale-to-zero on drain.** Drain the queue → after `cooldownPeriod` (300s)
   KEDA scales the worker Deployment back to 0.
4. **Liveness vs dependency blip.** `GET /healthz` stays 200 through a transient
   Postgres outage → the pod is not killed.

Also run `/security-review` on the manifests (exposed Services, Secret
handling, imagePullSecret, no api Ingress).
