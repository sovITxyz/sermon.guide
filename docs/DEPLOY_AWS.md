# Deploying sermon.guide to AWS (v0, single box)

Operator runbook for the v0 deployment: the whole stack — Postgres, Redis,
Milvus (+etcd +MinIO), FastAPI api, Celery worker, Next.js web, Caddy TLS
edge — on **one EC2 instance** via `infra/docker-compose.prod.yml`. This is
the dollar-store rendering of ARCHITECTURE.md §1's "~$50/mo" target; the
KEDA/k8s shape stays Phase 30.

## TL;DR

```bash
aws configure                       # once: credentials + region
cd infra/aws
./provision.sh                      # EC2 t3a.xlarge + EIP + SG (~2min)
./deploy.sh                         # clone, build, migrate, bootstrap, smoke (first run ~minutes)
                                    #   deploys your CURRENT branch — it must be pushed to origin
                                    #   (deploy.sh preflights this and tells you if not)
./stop.sh                           # done for the day → compute billing off
./start.sh                          # back up on the same IP in ~3min
./status.sh                         # state + URL + billing posture
./destroy.sh                        # everything gone, $0
```

The site serves at `https://<elastic-ip>` with a self-signed cert (Caddy's
internal CA) — the browser warns once per browser; proceed. No domain is
required; see [Adding a domain](#adding-a-domain-later) for the day one exists.

## Cost

| State | What bills | ≈/mo |
| --- | --- | --- |
| Running 24/7 | t3a.xlarge ($0.1504/hr us-east-1) + 100GB gp3 + EIP | ~$122 |
| Running 8h/day | compute ~⅓ + disk + EIP | ~$48 |
| **Stopped** | 100GB gp3 (~$8) + EIP (~$3.65) | **~$12** |

t3a is burstable (unlimited mode by default): sustained heavy CPU — e.g.
hours of ingest — can accrue small credit-overage charges; fine for bursty
beta use, watch CloudWatch `CPUSurplusCreditCharged` if you batch-ingest a
whole library. **Set a billing alarm** (Billing → Budgets) — nothing in this
stack does it for you.

LLM spend is separate (per-query, via `GOOGLE_API_KEY`/`PPQ_API_KEY`); a
warm `/search-summary` was ~6¢ in the Phase 14b live verify. Remote
inference spend (Phase 16b, `DEEPINFRA_API_KEY`) is per-call too:
~$0.006/book ingest, well under a cent per search. Set spend caps at both
providers.

## What deploy.sh actually does

1. **Code** → `git clone`/`reset --hard` of the chosen branch into `/opt/sermon/app`.
2. **Secrets** → first run generates `/opt/sermon/.env.prod` *on the box*
   (`openssl rand`; JWT secret, Postgres/Redis/MinIO passwords). Secrets never
   exist on a dev machine or in git. `docker-compose.prod.yml` uses `${VAR:?}`
   so a missing secret refuses to boot rather than falling back to the
   dev defaults baked into the code (the Phase 18 startup-guard gap, mitigated
   at the compose layer).
3. **API keys** → if `GOOGLE_API_KEY`/`PPQ_API_KEY`/`DEEPINFRA_API_KEY` are
   set in your local shell when you run `./deploy.sh`, they're forwarded into
   the box's env file (PPQ also flips the provider). Without an LLM key,
   everything works except `/search-summary`, which 503s naming the missing
   var. `DEEPINFRA_API_KEY` is REQUIRED (Phase 16b: every search and ingest
   runs remote inference) — deploy.sh aborts before the build if it's neither
   on the box nor forwarded.
4. **Build** → all four images build on the instance (first build is minutes
   now — Phase 16b removed the torch wheels; Next + xcaddy dominate).
5. **Bootstrap** → `alembic upgrade head`, `bootstrap_milvus.py` (both
   idempotent). No model downloads: all inference is remote (ADR 0006).
6. **Up + smoke** → `up -d --wait`, then an outside-in signup→login→/library
   pass through Caddy with a cookie jar.

## Security posture (read before sharing the URL)

Mitigated at deploy time, no app-code changes:

- **Only Caddy publishes ports** (80/443). Postgres/Redis/Milvus (which has
  *no auth*)/MinIO/etcd/api/web are compose-network-only; the security group
  (443/80 world, 22 admin-IP) is the backstop.
- **Strong generated secrets**; compose hard-fails if any is missing.
- **Per-IP rate limits at Caddy**: 10/min on `/api/auth/*`, 6/min on
  `/api/search-summary` + `/api/upload`, 600/min general (Phase 19 gap).
- **Body caps at the edge**: 1MB on JSON routes, 210MB global (the api's own
  200MB streamed cap is the real gate; this protects the Node proxy).
- **Celery `--time-limit=3600`** so one poisoned upload can't pin the CPU.
- **Secure session cookie**: web runs `NODE_ENV=production` (bakes the
  `Secure` attribute), everything redirects to HTTPS.
- The api is **not publicly reachable at all** — browsers only ever hit the
  Next route handlers, which attach the JWT server-side.

Accepted v0 risks (documented in the Phase 17–30 plan, revisit before real
launch): open signup (rate-limited but no email verification/CAPTCHA),
`task_id`-as-capability on `/tasks/{id}`, no Pydantic `extra='forbid'`,
no graceful degradation (a Milvus blip = 500), CPU latency (warm
`/search-summary` ≈ 2min — "user is reading, not chatting").

## Day-2 operations

```bash
# logs (whole stack / one service)
ssh -i ~/.ssh/sermon-guide.pem ubuntu@<ip>
cd /opt/sermon/app
docker compose -f infra/docker-compose.prod.yml --env-file /opt/sermon/.env.prod logs -f [api|worker|web|caddy|milvus]

# redeploy after pushing changes to the branch
./deploy.sh                      # re-runs build/migrate/bootstrap idempotently

# add or rotate the LLM key later
ssh … 'sed -i "s|^GOOGLE_API_KEY=.*|GOOGLE_API_KEY=<key>|" /opt/sermon/.env.prod'
ssh … 'cd /opt/sermon/app && docker compose -f infra/docker-compose.prod.yml --env-file /opt/sermon/.env.prod up -d api'

# manual backup before risky changes (Phase 28 will do this properly)
aws ec2 create-snapshot --volume-id $(aws ec2 describe-instances \
  --filters Name=tag:Name,Values=sermon-guide Name=instance-state-name,Values=running,stopped \
  --query 'Reservations[0].Instances[0].BlockDeviceMappings[0].Ebs.VolumeId' --output text) \
  --description "sermon-guide manual backup"
```

## Adding a domain later

1. Point an A record at the Elastic IP.
2. On the box, edit `/opt/sermon/.env.prod`:
   `SITE_HOST=sermon.guide` and
   `SERMON_API_CORS_ORIGINS=["https://sermon.guide"]`.
3. Remove the `tls internal` line from `infra/caddy/Caddyfile` (commit that
   change) so automatic Let's Encrypt takes over.
4. `docker compose … up -d caddy api`. Add an HSTS header in the Caddyfile
   once the real cert is confirmed working (deliberately absent now — HSTS on
   a self-signed IP would lock browsers out).

## Known deltas vs the phase plan

- This is operator tooling on branch `deploy/aws-v0`, not Phase 29/30:
  images build on the box (no registry/CI). When Phase 29 lands proper
  image-build CI, these Dockerfiles are its starting point. (The model
  volume + `prewarm` step this bullet used to describe were removed by
  Phase 16b — all inference is remote now, ADR 0006.)
- `web/next.config.ts` gained `output: "standalone"` (required for the slim
  web image; dev/CI behavior unchanged).
