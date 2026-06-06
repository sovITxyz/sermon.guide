#!/usr/bin/env bash
# Deploy (or re-deploy) sermon.guide onto the provisioned EC2 box.
#
#   ./deploy.sh             # deploys the branch you're currently on
#   BRANCH=main ./deploy.sh # deploy a specific branch
#
# What it does, in order (every step idempotent, safe to re-run):
#   1. clone/pull the repo on the instance (/opt/sermon/app)
#   2. first run only: generate /opt/sermon/.env.prod — strong secrets are
#      created ON the box with openssl and never leave it
#   3. forward GOOGLE_API_KEY / PPQ_API_KEY from the local env if set
#      (so the LLM key never lands in git or chat either). Keys are STICKY:
#      once set on the box they persist across deploys until you edit
#      /opt/sermon/.env.prod by hand — running deploy.sh without the var
#      exported leaves the old key (and provider) in place.
#   4. docker compose build (first build ~10-20min: torch wheels, Next build,
#      xcaddy compile)
#   5. up the data plane, run one-shots: migrate → bootstrap-milvus → prewarm
#      (prewarm downloads ~3.7GB of models into the hf-cache volume once)
#   6. up everything, then smoke-test from the OUTSIDE (signup→login→library
#      through Caddy with a cookie jar)

. "$(dirname "$0")/common.sh"

REPO_URL="${REPO_URL:-https://github.com/sovITxyz/sermon.guide.git}"
BRANCH="${BRANCH:-$(git -C "$(dirname "$0")/../.." rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)}"

# Preflight: the box clones BRANCH from origin over anonymous HTTPS. If the
# branch isn't pushed, the remote `git clone --branch` fails with a cryptic
# exit-128 mid-SSH — catch it here with an actionable message instead.
GIT_TERMINAL_PROMPT=0 git ls-remote --exit-code --heads "${REPO_URL}" "refs/heads/${BRANCH}" >/dev/null 2>&1 \
  || die "branch '${BRANCH}' is not on origin (${REPO_URL}) — push it first: git push -u origin ${BRANCH}"

require_aws

instance_id="$(find_instance)"
[ -n "${instance_id}" ] || die "no instance found — run provision.sh first"
state="$(instance_state "${instance_id}")"
[ "${state}" = "running" ] || die "instance ${instance_id} is ${state} — run start.sh first"

eip_info="$(find_eip)"
[ -n "${eip_info}" ] || die "no Elastic IP tagged ${TAG_NAME}"
ip="$(echo "${eip_info}" | awk '{print $2}')"

echo "deploying branch '${BRANCH}' to ${instance_id} @ ${ip}"
wait_for_ssh "${ip}"

# Wait for cloud-init (Docker install) on a fresh box.
ssh_cmd "${ip}" "cloud-init status --wait >/dev/null 2>&1 || true"
ssh_cmd "${ip}" "command -v docker >/dev/null" || die "Docker missing on instance — cloud-init failed? check /var/log/cloud-init-output.log"

# Optional LLM keys forwarded from the local environment (never stored in git).
llm_env=""
[ -n "${GOOGLE_API_KEY:-}" ] && llm_env="GOOGLE_API_KEY=${GOOGLE_API_KEY}"
[ -n "${PPQ_API_KEY:-}" ] && llm_env="${llm_env} PPQ_API_KEY=${PPQ_API_KEY}"

ssh_cmd "${ip}" "bash -s" <<REMOTE
set -euo pipefail
export ${llm_env:-_NOOP=1}

# --- 1. code ---
if [ ! -d /opt/sermon/app/.git ]; then
  git clone --branch "${BRANCH}" "${REPO_URL}" /opt/sermon/app
else
  git -C /opt/sermon/app fetch origin
  git -C /opt/sermon/app checkout "${BRANCH}"
  git -C /opt/sermon/app reset --hard "origin/${BRANCH}"
fi

# --- 2. env file (first run only; secrets generated on-box) ---
if [ ! -f /opt/sermon/.env.prod ]; then
  umask 177
  cat > /opt/sermon/.env.prod <<EOF
SERMON_API_JWT_SECRET=\$(openssl rand -hex 48)
SERMON_POSTGRES_PASSWORD=\$(openssl rand -hex 24)
SERMON_REDIS_PASSWORD=\$(openssl rand -hex 24)
SERMON_MINIO_ROOT_PASSWORD=\$(openssl rand -hex 24)
SERMON_POSTGRES_USER=sermon
SERMON_POSTGRES_DB=sermon
SERMON_MINIO_ROOT_USER=sermon-minio
SITE_HOST=${ip}
SERMON_API_CORS_ORIGINS=["https://${ip}"]
SERMON_API_LLM_PROVIDER=google
SERMON_API_LLM_MODEL=
GOOGLE_API_KEY=
PPQ_API_KEY=
EOF
  umask 022
  echo "generated /opt/sermon/.env.prod"
fi

# --- 3. LLM keys forwarded from the operator's shell, if any ---
# Values are written via shell vars + redirects only — never as argv of
# sed/etc., so a secret can't appear in the box's process list mid-write.
set_kv() {
  ( umask 177; { grep -v "^\$1=" /opt/sermon/.env.prod; printf '%s=%s\n' "\$1" "\$2"; } > /opt/sermon/.env.prod.new )
  mv /opt/sermon/.env.prod.new /opt/sermon/.env.prod
}
if [ -n "\${GOOGLE_API_KEY:-}" ]; then
  set_kv GOOGLE_API_KEY "\${GOOGLE_API_KEY}"
  echo "GOOGLE_API_KEY updated"
fi
if [ -n "\${PPQ_API_KEY:-}" ]; then
  set_kv PPQ_API_KEY "\${PPQ_API_KEY}"
  set_kv SERMON_API_LLM_PROVIDER ppq
  echo "PPQ_API_KEY updated (provider → ppq)"
fi

cd /opt/sermon/app
compose() {
  docker compose -f infra/docker-compose.prod.yml --env-file /opt/sermon/.env.prod "\$@"
}

# --- 4. build ---
compose build

# --- 5. data plane up → one-shots ---
compose up -d --wait postgres redis etcd minio milvus
compose run --rm migrate
# Milvus's :9091 healthz can report healthy moments before the :19530 gRPC
# path accepts queries on a cold standalone — retry instead of aborting the
# whole deploy on that race.
for attempt in 1 2 3; do
  if compose run --rm bootstrap-milvus; then
    break
  fi
  if [ "\${attempt}" = 3 ]; then
    echo "bootstrap-milvus failed after 3 attempts" >&2
    exit 1
  fi
  echo "bootstrap-milvus attempt \${attempt} failed (cold Milvus gRPC?) — retrying in 10s"
  sleep 10
done

# Prewarm only when the heaviest model isn't cached yet (~3.7GB once).
if ! docker run --rm -v sermon_sermon-hf-cache:/hf-cache alpine:3.20 \
    test -d /hf-cache/hub/models--BAAI--bge-m3 2>/dev/null; then
  compose run --rm prewarm
else
  echo "hf-cache already warm — skipping prewarm"
fi

# --- 6. full stack ---
compose up -d --wait
compose ps
REMOTE

# --- smoke test from the outside, through Caddy ---
echo
echo "smoke-testing https://${ip} …"
jar="$(mktemp)"
trap 'rm -f "${jar}"' EXIT

# / 307s to /library, which 307s to /login without a cookie (app/page.tsx +
# middleware.ts) — follow the chain and assert the final page serves.
code="$(curl -skL -o /dev/null -w '%{http_code}' "https://${ip}/")"
echo "  GET / (-L)       → ${code}"
[ "${code}" = "200" ] || die "landing page not serving"

# Fresh throwaway user per deploy so the FULL authed path (signup → login →
# cookie → authed page) is asserted on every run, not just the first; any
# unexpected code (500/502/503/429) is a hard failure, not a silent skip.
# Domain must be @example.com — the api's email validation 422s reserved
# TLDs like .test (verified against the live stack).
smoke_user="smoke-$(openssl rand -hex 4)@example.com"
smoke_pw="smoke-$(openssl rand -hex 8)"
code="$(curl -sk -o /dev/null -w '%{http_code}' -X POST "https://${ip}/api/auth/signup" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"${smoke_user}\",\"password\":\"${smoke_pw}\"}")"
echo "  POST signup      → ${code}"
[ "${code}" = "201" ] || die "signup returned ${code} — api/postgres path not healthy"

code="$(curl -sk -c "${jar}" -o /dev/null -w '%{http_code}' -X POST "https://${ip}/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"${smoke_user}\",\"password\":\"${smoke_pw}\"}")"
echo "  POST login       → ${code}"
[ "${code}" = "200" ] || die "login failed"
grep -q sg_session "${jar}" || die "session cookie not set"

code="$(curl -sk -b "${jar}" -o /dev/null -w '%{http_code}' "https://${ip}/library")"
echo "  GET /library     → ${code} (authed)"
[ "${code}" = "200" ] || die "authed library page failed"

code="$(curl -sk -o /dev/null -w '%{http_code}' "https://${ip}/library")"
echo "  GET /library     → ${code} (no cookie; 307 → /login expected)"

code="$(curl -sk -o /dev/null -w '%{http_code}' -X POST "https://${ip}/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"${smoke_user}\",\"password\":\"wrong-password\"}")"
echo "  bad login        → ${code} (401 expected)"
[ "${code}" = "401" ] || die "bad credentials did not 401"

echo
echo "deployed ✓  https://${ip}"
echo "  (self-signed cert — browser will warn once; Accept/Proceed is expected)"
echo "  logs:    ssh -i ${KEY_FILE} ${SSH_USER}@${ip} 'cd /opt/sermon/app && docker compose -f infra/docker-compose.prod.yml --env-file /opt/sermon/.env.prod logs -f'"
echo "  stop:    ./stop.sh   (compute billing stops; disk+IP ≈ \$12/mo)"
