#!/usr/bin/env bash
# sermon.guide — full data-plane backup (Phase 28).
#
#   make backup                 # -> infra/backups/<UTC timestamp>/
#   BACKUP_DIR=/mnt/x make backup
#
# Backs up all three stateful stores to a HOST directory (never a docker
# volume — `make nuke` wipes volumes). Legs, in order:
#   1. Postgres   — pg_dump custom format (-Fc) of the whole app DB
#   2. Milvus     — milvus-backup create -> then mc-mirror its MinIO output to host
#   3. MinIO app  — mc mirror of the originals bucket (raw uploads)
#
# Every artifact lands under BACKUP_DIR/<timestamp>/. The run is non-destructive:
# it only READS from the live stack. Restore is the inverse — see restore.sh
# and docs/BACKUP_RESTORE.md.

set -euo pipefail
# shellcheck source=infra/scripts/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

load_env
require_docker
require_container "${PG_CONTAINER}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${BACKUP_DIR}/${STAMP}"
mkdir -p "${DEST}/postgres" "${DEST}/milvus" "${DEST}/minio"

log "backup target: ${DEST}"
log "BACKUP_DIR=${BACKUP_DIR} (host path; gitignored)"

# --- 1. Postgres -------------------------------------------------------------
# Custom format (-Fc): compressed, restored with pg_restore. PGPASSWORD is
# passed into the container env only (never echoed). Dump streams to the host
# file over the docker exec stdout pipe so it lands directly under BACKUP_DIR.
log "[1/3] pg_dump (custom format) -> postgres/sermon.dump"
docker exec -e PGPASSWORD="${SERMON_POSTGRES_PASSWORD}" "${PG_CONTAINER}" \
  pg_dump -U "${SERMON_POSTGRES_USER}" -d "${SERMON_POSTGRES_DB}" \
  -Fc --no-owner --no-privileges \
  > "${DEST}/postgres/sermon.dump"
pg_bytes="$(stat -c%s "${DEST}/postgres/sermon.dump")"
[ "${pg_bytes}" -gt 1024 ] || die "pg_dump produced a suspiciously small file (${pg_bytes} bytes)"
log "      pg dump: $(numfmt --to=iec "${pg_bytes}")"

# --- 2. Milvus ---------------------------------------------------------------
# milvus-backup writes INTO MinIO at a-bucket/backup/<name> (inside the
# sermon-minio VOLUME, which `make nuke` destroys), so we immediately mirror
# that output out to the host. The backup name mirrors the timestamp so the
# host artifact and the in-MinIO copy are traceable to each other.
MB_NAME="sermon_${STAMP}"
log "[2/3] milvus-backup create -n ${MB_NAME} (collection ${MILVUS_COLLECTION})"
run_milvus_backup create -n "${MB_NAME}" --filter "${MILVUS_COLLECTION}" >/dev/null
log "      mirroring ${MILVUS_DATA_BUCKET}/${MILVUS_BACKUP_ROOTPATH}/${MB_NAME} -> milvus/${MB_NAME}"
run_mc "mc mirror --quiet --overwrite m/${MILVUS_DATA_BUCKET}/${MILVUS_BACKUP_ROOTPATH}/${MB_NAME} /host/${MB_NAME}" \
  "${DEST}/milvus"
# Record the backup name so restore.sh knows what to feed milvus-backup.
echo "${MB_NAME}" > "${DEST}/milvus/BACKUP_NAME"
# Free the in-MinIO copy now that it is safely on the host (keeps the live
# a-bucket lean; the host copy is the source of truth for restore).
run_milvus_backup delete -n "${MB_NAME}" >/dev/null 2>&1 || \
  warn "could not delete in-MinIO milvus backup ${MB_NAME} (host copy is intact)"
milvus_bytes="$(find "${DEST}/milvus/${MB_NAME}" -type f -exec stat -c%s {} \; 2>/dev/null | awk '{s+=$1} END {print s+0}')"
[ "${milvus_bytes}" -gt 1024 ] || die "milvus backup mirror produced almost nothing (${milvus_bytes} bytes)"
log "      milvus backup: $(numfmt --to=iec "${milvus_bytes}")"

# --- 3. MinIO app buckets ----------------------------------------------------
# The originals bucket (Phase 31 raw EPUB/PDF uploads) is independent of Milvus
# and not captured by milvus-backup, so mirror it separately. a-bucket/files is
# Milvus's own segment store and is NOT mirrored here — it is reconstructed by
# milvus-backup restore, so copying it raw would be redundant and restore-fragile.
log "[3/3] mc mirror originals bucket (${SERMON_MINIO_ORIGINALS_BUCKET}) -> minio/${SERMON_MINIO_ORIGINALS_BUCKET}"
run_mc "mc mb --ignore-existing m/${SERMON_MINIO_ORIGINALS_BUCKET} >/dev/null 2>&1; mc mirror --quiet --overwrite m/${SERMON_MINIO_ORIGINALS_BUCKET} /host/${SERMON_MINIO_ORIGINALS_BUCKET}" \
  "${DEST}/minio"
minio_bytes="$(find "${DEST}/minio/${SERMON_MINIO_ORIGINALS_BUCKET}" -type f -exec stat -c%s {} \; 2>/dev/null | awk '{s+=$1} END {print s+0}')"
log "      originals: $(numfmt --to=iec "${minio_bytes}")"

# --- manifest ----------------------------------------------------------------
# A tiny manifest records what stack/tool versions produced this backup, so a
# future restore can sanity-check compatibility (milvus-backup only restores to
# the same-or-newer Milvus).
cat > "${DEST}/MANIFEST.txt" <<EOF
sermon.guide backup
created_utc=${STAMP}
milvus_collection=${MILVUS_COLLECTION}
milvus_backup_name=${MB_NAME}
milvus_backup_tool=v${MILVUS_BACKUP_VERSION}
milvus_image=milvusdb/milvus:v2.6.15
postgres_image=postgres:16-alpine
minio_originals_bucket=${SERMON_MINIO_ORIGINALS_BUCKET}
EOF

# --- update the "latest" pointer ---------------------------------------------
# restore.sh defaults to the newest backup; a symlink makes that explicit and
# scriptable. Recreated each run.
ln -sfn "${STAMP}" "${BACKUP_DIR}/latest"

log "DONE. backup at ${DEST}"
log "inventory:"
( cd "${DEST}" && du -sh postgres milvus minio MANIFEST.txt 2>/dev/null )
log "off-box copy (documented, not run): rsync -a --delete ${BACKUP_DIR}/ user@host:/srv/sermon-backups/"
