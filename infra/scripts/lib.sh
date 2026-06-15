#!/usr/bin/env bash
# Shared helpers for the sermon.guide backup/restore scripts (Phase 28).
# Source, don't execute.
#
# These wrap the compose data plane (Postgres + Milvus's MinIO/etcd + the app
# MinIO buckets) so `make backup` / `make restore` work from the repo root.
# See docs/BACKUP_RESTORE.md for the full runbook + restore drill.
#
# Conventions:
#   - infra/.env is the single source of creds. We SOURCE it (set -a) and
#     NEVER echo a secret value. Callers must keep it that way.
#   - All artifacts land under BACKUP_DIR, a HOST path (default infra/backups/,
#     gitignored). NEVER inside a docker volume — `make nuke` wipes volumes, so
#     a backup there would die with the data it protects.
#   - We talk to the live containers over the compose network `sermon_default`
#     (one-shot helper containers) or via `docker exec` into the named
#     containers. No host-side client tools are required beyond docker + curl.

set -euo pipefail

# --- resolve paths -----------------------------------------------------------
# This file lives at infra/scripts/lib.sh; REPO_ROOT is two dirs up.
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "${LIB_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${INFRA_DIR}/.." && pwd)"

ENV_FILE="${INFRA_DIR}/.env"

# BACKUP_DIR: host path, env-overridable, default infra/backups/ (gitignored).
# Relative overrides are resolved against the repo root so `BACKUP_DIR=foo`
# never lands somewhere surprising depending on the caller's cwd.
BACKUP_DIR="${BACKUP_DIR:-${INFRA_DIR}/backups}"
case "${BACKUP_DIR}" in
  /*) : ;;                              # already absolute
  *)  BACKUP_DIR="${REPO_ROOT}/${BACKUP_DIR}" ;;
esac

# --- pinned tool versions ----------------------------------------------------
# milvus-backup: the official zilliztech tool. v0.5.16 is the supported path
# for Milvus 2.6.15 — NEVER pair this stack with < v0.5.11 (the < v0.5.11
# FlushAll bug can CORRUPT metadata on Milvus 2.6.9/2.6.10; v0.5.11 raised the
# FlushAll floor to 2.6.11, ours is 2.6.15 so v0.5.16 is safe). See the runbook.
MILVUS_BACKUP_VERSION="${MILVUS_BACKUP_VERSION:-0.5.16}"
MILVUS_BACKUP_SHA256="8c05a61ab3c2e73e590816734079e2b58004b64d26de4e04e6af01ad91d2cf42"
MILVUS_BACKUP_URL="https://github.com/zilliztech/milvus-backup/releases/download/v${MILVUS_BACKUP_VERSION}/milvus-backup_${MILVUS_BACKUP_VERSION}_Linux_x86_64.tar.gz"

# Cached binary lives under BACKUP_DIR/.tools so we download it once and reuse
# it across runs (it is NOT a backup artifact; the dir is gitignored anyway).
TOOLS_DIR="${BACKUP_DIR}/.tools"
MILVUS_BACKUP_BIN="${TOOLS_DIR}/milvus-backup-${MILVUS_BACKUP_VERSION}"

# Helper container images — all already pulled by the compose stack, so no
# surprise pulls during a backup/restore.
MC_IMAGE="minio/mc:RELEASE.2024-06-12T14-34-03Z"
RUNNER_IMAGE="alpine:3.20"  # hosts the static milvus-backup binary

# --- compose / stack facts (match infra/docker-compose.yml) ------------------
COMPOSE_NETWORK="sermon_default"
PG_CONTAINER="sermon-postgres"

MILVUS_COLLECTION="library_vectors"   # worker/scripts/bootstrap_milvus.py:24
MILVUS_DATA_BUCKET="a-bucket"         # Milvus object store (rootPath files/)
MILVUS_BACKUP_ROOTPATH="backup"       # milvus-backup writes here, inside a-bucket

# --- logging -----------------------------------------------------------------
log()  { echo "[$(date -u +%H:%M:%S)] $*"; }
warn() { echo "[$(date -u +%H:%M:%S)] WARN: $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

# --- env / preflight ---------------------------------------------------------
# Source infra/.env into the environment WITHOUT printing any value.
load_env() {
  [ -f "${ENV_FILE}" ] || die "infra/.env not found — run 'make env' / 'make up' first"
  set -a
  # shellcheck disable=SC1090
  . "${ENV_FILE}"
  set +a
  : "${SERMON_POSTGRES_USER:?SERMON_POSTGRES_USER missing from infra/.env}"
  : "${SERMON_POSTGRES_PASSWORD:?SERMON_POSTGRES_PASSWORD missing from infra/.env}"
  : "${SERMON_POSTGRES_DB:?SERMON_POSTGRES_DB missing from infra/.env}"
  : "${SERMON_MINIO_ROOT_USER:?SERMON_MINIO_ROOT_USER missing from infra/.env}"
  : "${SERMON_MINIO_ROOT_PASSWORD:?SERMON_MINIO_ROOT_PASSWORD missing from infra/.env}"
  # Originals bucket: default to the documented name (.env.example /
  # worker default) so a pre-Phase-31 infra/.env without this key still works.
  SERMON_MINIO_ORIGINALS_BUCKET="${SERMON_MINIO_ORIGINALS_BUCKET:-sermon-originals}"
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "docker not found on PATH"
  docker network inspect "${COMPOSE_NETWORK}" >/dev/null 2>&1 \
    || die "compose network '${COMPOSE_NETWORK}' not found — is the stack up? run 'make up'"
}

require_container() {
  docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true \
    || die "container '$1' is not running — run 'make up' first"
}

# --- milvus-backup binary management -----------------------------------------
# Download + checksum-verify the pinned milvus-backup binary once, cache it.
ensure_milvus_backup_bin() {
  if [ -x "${MILVUS_BACKUP_BIN}" ]; then
    return 0
  fi
  mkdir -p "${TOOLS_DIR}"
  log "fetching milvus-backup v${MILVUS_BACKUP_VERSION} (one-time, cached under BACKUP_DIR/.tools)"
  local tgz="${TOOLS_DIR}/milvus-backup-${MILVUS_BACKUP_VERSION}.tar.gz"
  curl -fsSL -o "${tgz}" "${MILVUS_BACKUP_URL}" \
    || die "download failed: ${MILVUS_BACKUP_URL}"
  local actual
  actual="$(sha256sum "${tgz}" | awk '{print $1}')"
  [ "${actual}" = "${MILVUS_BACKUP_SHA256}" ] \
    || die "milvus-backup checksum mismatch: expected ${MILVUS_BACKUP_SHA256}, got ${actual}"
  tar -xzf "${tgz}" -C "${TOOLS_DIR}" milvus-backup
  mv "${TOOLS_DIR}/milvus-backup" "${MILVUS_BACKUP_BIN}"
  chmod +x "${MILVUS_BACKUP_BIN}"
  rm -f "${tgz}"
  log "milvus-backup v${MILVUS_BACKUP_VERSION} ready (sha256 verified)"
}

# Run the milvus-backup binary in a one-shot container on the compose network.
# Creds are injected via --set (dotted lowercase keys) so NO secret is ever
# written to the on-disk backup.yaml. Args after the function name are passed
# straight through to the binary (e.g. create -n NAME --filter COLL).
run_milvus_backup() {
  ensure_milvus_backup_bin
  docker run --rm --network "${COMPOSE_NETWORK}" \
    -v "${MILVUS_BACKUP_BIN}:/milvus-backup:ro" \
    -v "${INFRA_DIR}/backup/backup.yaml:/backup.yaml:ro" \
    -w / \
    "${RUNNER_IMAGE}" /milvus-backup "$@" \
    --set minio.accessKeyID="${SERMON_MINIO_ROOT_USER}" \
    --set minio.secretAccessKey="${SERMON_MINIO_ROOT_PASSWORD}"
}

# Run an `mc` script against the compose MinIO. The script body is passed as
# $1 and runs inside the mc container with alias `m` already configured and an
# optional host bind mount at /host (caller passes the host path as $2).
#
# Creds are handed to the container as ENV VARS (-e) and the alias is set from
# $MC_USER/$MC_PASS inside the container, so the secret values are NEVER
# interpolated into the `sh -c` command string (no injection from a password
# with shell metacharacters, and nothing secret lands in the container's argv).
run_mc() {
  local script="$1" hostmount="${2:-}"
  local args=(run --rm --network "${COMPOSE_NETWORK}" --entrypoint sh
    -e "MC_USER=${SERMON_MINIO_ROOT_USER}"
    -e "MC_PASS=${SERMON_MINIO_ROOT_PASSWORD}")
  if [ -n "${hostmount}" ]; then
    mkdir -p "${hostmount}"
    args+=(-v "${hostmount}:/host")
  fi
  args+=("${MC_IMAGE}" -c "
    mc alias set m http://minio:9000 \"\$MC_USER\" \"\$MC_PASS\" >/dev/null 2>&1
    ${script}
  ")
  docker "${args[@]}"
}
