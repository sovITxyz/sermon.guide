#!/usr/bin/env bash
# sermon.guide — full data-plane restore (Phase 28).
#
#   make restore                       # restore the newest backup (infra/backups/latest)
#   make restore BACKUP=20260615T112121Z
#   BACKUP_DIR=/mnt/x make restore BACKUP=...
#
# Inverse of backup.sh. Assumes a FRESH, UP stack (the drill is: backup ->
# make nuke -> make up -> make restore). Legs, in order:
#   1. Postgres   — pg_restore the custom-format dump into the (fresh) DB
#   2. MinIO app  — mc mirror the originals bucket back in
#   3. Milvus     — mirror the host milvus backup back INTO MinIO, then
#                   milvus-backup restore (recreates the collection + data)
#
# Postgres + MinIO first, Milvus last: milvus-backup restore re-imports THROUGH
# a running Milvus and recreates the collection from the backup's own schema, so
# it needs Milvus up but does NOT need bootstrap_milvus.py to have run first.
# See docs/BACKUP_RESTORE.md for the full drill + verify steps.

set -euo pipefail
# shellcheck source=infra/scripts/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

load_env
require_docker
require_container "${PG_CONTAINER}"

# --- pick which backup to restore --------------------------------------------
BACKUP="${BACKUP:-}"
if [ -z "${BACKUP}" ]; then
  [ -e "${BACKUP_DIR}/latest" ] || die "no BACKUP given and no '${BACKUP_DIR}/latest' pointer — pass BACKUP=<timestamp>"
  BACKUP="$(readlink "${BACKUP_DIR}/latest")"
fi
SRC="${BACKUP_DIR}/${BACKUP}"
[ -d "${SRC}" ] || die "backup not found: ${SRC}"
[ -f "${SRC}/postgres/sermon.dump" ] || die "missing ${SRC}/postgres/sermon.dump"
[ -f "${SRC}/milvus/BACKUP_NAME" ]   || die "missing ${SRC}/milvus/BACKUP_NAME"
MB_NAME="$(cat "${SRC}/milvus/BACKUP_NAME")"

log "restoring from ${SRC}"
[ -f "${SRC}/MANIFEST.txt" ] && { log "manifest:"; sed 's/^/      /' "${SRC}/MANIFEST.txt"; }

# --- 1. Postgres -------------------------------------------------------------
# Stream the host dump into pg_restore inside the container. --clean
# --if-exists makes the restore idempotent-ish: it drops + recreates each
# object, so restoring over a partially-populated DB still converges. On a
# truly fresh DB (post-nuke) the DROPs are harmless no-ops.
log "[1/3] pg_restore -> ${SERMON_POSTGRES_DB}"
docker exec -i -e PGPASSWORD="${SERMON_POSTGRES_PASSWORD}" "${PG_CONTAINER}" \
  pg_restore -U "${SERMON_POSTGRES_USER}" -d "${SERMON_POSTGRES_DB}" \
  --clean --if-exists --no-owner --no-privileges \
  < "${SRC}/postgres/sermon.dump" 2>&1 | grep -vE '^$' | tail -5 || true
log "      postgres restored"

# --- 2. MinIO app buckets ----------------------------------------------------
log "[2/3] mc mirror originals (${SERMON_MINIO_ORIGINALS_BUCKET}) back into MinIO"
if [ -d "${SRC}/minio/${SERMON_MINIO_ORIGINALS_BUCKET}" ]; then
  run_mc "mc mb --ignore-existing m/${SERMON_MINIO_ORIGINALS_BUCKET} >/dev/null 2>&1; mc mirror --quiet --overwrite /host/${SERMON_MINIO_ORIGINALS_BUCKET} m/${SERMON_MINIO_ORIGINALS_BUCKET}" \
    "${SRC}/minio"
  log "      originals restored"
else
  warn "no originals bucket in backup — skipping"
fi

# --- 3. Milvus ---------------------------------------------------------------
# milvus-backup reads the backup FROM MinIO (a-bucket/backup/<name>), so first
# mirror the host artifact back into the freshly-upped MinIO, then restore.
# --drop_exist_collection lets us restore under the ORIGINAL collection name
# even if a (post-nuke empty) collection already exists, so /search sees the
# real name with no rename juggling.
log "[3/3] milvus-backup restore -n ${MB_NAME} -> ${MILVUS_COLLECTION}"
log "      mirroring host milvus/${MB_NAME} -> ${MILVUS_DATA_BUCKET}/${MILVUS_BACKUP_ROOTPATH}/${MB_NAME}"
run_mc "mc mb --ignore-existing m/${MILVUS_DATA_BUCKET} >/dev/null 2>&1; mc mirror --quiet --overwrite /host/${MB_NAME} m/${MILVUS_DATA_BUCKET}/${MILVUS_BACKUP_ROOTPATH}/${MB_NAME}" \
  "${SRC}/milvus"
run_milvus_backup restore -n "${MB_NAME}" --filter "${MILVUS_COLLECTION}" --drop_exist_collection --rebuild_index >/dev/null
# Clean the in-MinIO backup copy now that the restore consumed it.
run_milvus_backup delete -n "${MB_NAME}" >/dev/null 2>&1 || true
log "      milvus collection ${MILVUS_COLLECTION} restored"

log "DONE. restored backup ${BACKUP}"
log "NEXT: verify — see docs/BACKUP_RESTORE.md (load the collection, run /search,"
log "      then 'make test-isolation' in worker/ must pass 3/3)."
