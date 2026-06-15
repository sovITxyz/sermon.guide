# Backing up & restoring sermon.guide (v0, single box)

Operator runbook for the three stateful stores — **Postgres** (users, library,
chunks), **Milvus** (the `library_vectors` embeddings, via its etcd + MinIO
deps), and the **MinIO originals bucket** (raw EPUB/PDF uploads). Phase 28.

Losing the box must not mean losing everything. `make backup` snapshots all
three to a host directory; `make restore` rebuilds them. The restore **drill**
at the bottom is the proof: backup → `make nuke` → `make up` → `make restore` →
a round-tripped book is searchable again and tenant isolation survives.

## TL;DR

```bash
make backup                          # -> infra/backups/<UTC timestamp>/  (non-destructive)
# ... disaster ...
make up                              # bring a fresh stack up (auto-creates empty stores)
make restore                         # restore the newest backup (infra/backups/latest)
make restore BACKUP=20260615T112809Z # or a specific one
```

Everything reads creds from `infra/.env` (never printed) and writes under
`BACKUP_DIR` (default `infra/backups/`, **gitignored**). Override with
`BACKUP_DIR=/mnt/disk make backup`.

## What gets backed up, and how

| Store | Tool | Artifact under `BACKUP_DIR/<stamp>/` |
| --- | --- | --- |
| Postgres | `pg_dump -Fc` (custom format) in `sermon-postgres` | `postgres/sermon.dump` |
| Milvus `library_vectors` | `milvus-backup create` → `mc mirror` to host | `milvus/<name>/` (+ `BACKUP_NAME`) |
| MinIO originals bucket | `mc mirror` | `minio/sermon-originals/` |
| (provenance) | — | `MANIFEST.txt` |

A `latest` symlink points at the newest timestamp. The cached `milvus-backup`
binary lives under `BACKUP_DIR/.tools/` (downloaded + sha256-verified once).

### Why milvus-backup (the chosen Milvus path)

We use the official **[zilliztech/milvus-backup](https://github.com/zilliztech/milvus-backup)**
tool, pinned to **v0.5.16**, run as a one-shot container on the compose network
`sermon_default` (the binary is a static Linux x86_64 ELF; we host it in the
already-pulled `alpine:3.20`). This is the *supported, consistency-aware* path
for Milvus 2.6 — it pauses per-collection GC, flushes, and copies segment
binlogs as an internally consistent set, then re-imports them **through** a
running Milvus on restore.

> **Version safety (do not regress):** milvus-backup **< v0.5.11** has a
> `FlushAll` bug that can **corrupt metadata** on Milvus 2.6.9/2.6.10. v0.5.11
> raised the FlushAll floor to Milvus 2.6.11; our Milvus is **v2.6.15**, so
> v0.5.16 is safe. Never pair this stack with milvus-backup < v0.5.11. The pin
> + sha256 live in `infra/scripts/lib.sh`.

**The load-bearing gotcha:** milvus-backup does **not** write to the host
filesystem. It writes the backup *into the same MinIO* under
`a-bucket/backup/<name>` — which lives inside the `sermon-minio` docker
**volume** that `make nuke` destroys. So the backup step is always followed by
an `mc mirror` of that prefix **out to the host** `BACKUP_DIR`; on restore we
mirror it **back into** a freshly-upped MinIO before invoking
`milvus-backup restore`. `backup.sh` then deletes the in-MinIO copy so the live
`a-bucket` stays lean — the host copy is the source of truth.

> **Raw fallback (documented, not used):** backing up `a-bucket/files` (Milvus
> segments) + the etcd `by-dev/*` keyspace directly is *possible*
> (`docker exec sermon-etcd etcdctl snapshot save` produces a valid snapshot,
> `mc mirror` copies the bucket), but for 2.6 it is restore-fragile: segment
> IDs, binlog paths and collection metadata in etcd must be byte-consistent
> with the MinIO segment files, so a raw copy is only safe while Milvus is
> **stopped/quiesced**, and restore means stopping Milvus, restoring etcd +
> bucket together, then restarting. Use only as a last-resort DR path.

### Why the MinIO `a-bucket/files` prefix is NOT mirrored raw

`a-bucket/files` is Milvus's own live segment store; it is reconstructed by
`milvus-backup restore` from the backup we already took. Mirroring it raw too
would be redundant and restore-fragile (same consistency caveat as above). We
mirror only the **independent** `sermon-originals` bucket (Phase 31 uploads),
which Milvus knows nothing about.

## BACKUP_DIR & off-box copies

- Default: `infra/backups/` on the host — **gitignored** (`infra/.gitignore`),
  because artifacts contain real user data / PII. Never commit them.
- It is a **host path on purpose**. A backup inside a docker volume would be
  destroyed by `make nuke` along with the data it is meant to protect.
- Override per run: `BACKUP_DIR=/mnt/backups make backup`. Relative overrides
  resolve against the repo root.
- Artifacts written by the helper containers (pg dump is your user; the
  `mc`/milvus-backup mirrors are **root**-owned). `sudo` may be needed to prune
  old backups by hand.

**Push off-box** (a backup that only lives on the box you might lose is not a
backup). This is documented, not automated — pick your destination:

```bash
# to another host over SSH
rsync -a --delete infra/backups/ user@offsite:/srv/sermon-backups/

# or to S3 (uses your aws profile)
aws s3 sync infra/backups/ s3://my-sermon-backups/ --profile sovit
```

## Restore order (and why)

`make restore` runs the legs in this order; the order matters:

1. **Postgres** — `pg_restore --clean --if-exists` into the (fresh) DB. On a
   post-nuke empty DB the DROPs are no-ops; on a dirty DB it converges
   (idempotent-ish).
2. **MinIO originals** — `mc mirror` the bucket back in.
3. **Milvus** — mirror the host milvus artifact back into MinIO, then
   `milvus-backup restore --drop_exist_collection --rebuild_index`. This
   re-imports **through** Milvus and **recreates the `library_vectors`
   collection from the backup's own saved schema** — so Milvus must be **up**,
   but you do **not** need to run `bootstrap_milvus.py` first.
   `--drop_exist_collection` lets it restore under the original name even if a
   post-nuke empty collection exists.

There is exactly one required `make up` between nuke and restore (step below).
No `make up` is needed *between* the restore legs.

---

## The restore drill (run this to prove backups work)

This is the Phase 28 verify. It is **destructive** (`make nuke` wipes all data)
— only run it when you mean to, and only after a fresh `make backup`.

### 1. Note the current state (so you can prove the round-trip)

```bash
set -a; . infra/.env; set +a
# Postgres row counts
docker exec -e PGPASSWORD="$SERMON_POSTGRES_PASSWORD" sermon-postgres \
  psql -U "$SERMON_POSTGRES_USER" -d "$SERMON_POSTGRES_DB" -t -c \
  "SELECT 'users',count(*) FROM users UNION ALL SELECT 'global_books',count(*) FROM global_books UNION ALL SELECT 'chunks',count(*) FROM chunks UNION ALL SELECT 'user_library',count(*) FROM user_library;"
# Milvus row count
docker run --rm --network sermon_default python:3.12-slim sh -c \
  "pip install -q pymilvus==2.6.* && python -c \"from pymilvus import MilvusClient; c=MilvusClient(uri='http://milvus:19530'); print(c.get_collection_stats('library_vectors'))\""
```

Pick a book in your library and remember it — you'll `/search` for it after
restore.

### 2. Back up

```bash
make backup
```

Confirm the artifacts landed (non-trivial sizes):

```bash
ls -R infra/backups/latest/
cat infra/backups/latest/MANIFEST.txt
```

### 3. Nuke

```bash
make nuke      # down -v: destroys all 5 named volumes. Irreversible.
```

### 4. Bring a fresh stack up

```bash
make up        # blocks until every service is healthy; stores are EMPTY here
```

### 5. Restore

```bash
make restore   # restores infra/backups/latest into the fresh stack
```

### 6. Verify

```bash
set -a; . infra/.env; set +a

# (a) Postgres counts match step 1
docker exec -e PGPASSWORD="$SERMON_POSTGRES_PASSWORD" sermon-postgres \
  psql -U "$SERMON_POSTGRES_USER" -d "$SERMON_POSTGRES_DB" -t -c \
  "SELECT count(*) FROM chunks; SELECT count(*) FROM user_library;"

# (b) Milvus rows are back and the collection loads
docker run --rm --network sermon_default python:3.12-slim sh -c \
  "pip install -q pymilvus==2.6.* && python -c \"from pymilvus import MilvusClient; c=MilvusClient(uri='http://milvus:19530'); c.load_collection('library_vectors'); print(c.get_collection_stats('library_vectors'))\""

# (c) End-to-end: /search finds the book you noted in step 1
#     (signup/login then POST /search through the api — your usual smoke flow)

# (d) GATE — tenant scoping survived recovery: must pass 3/3
cd worker && make test-isolation
```

Restore passes when (a) the counts match step 1, (b) Milvus reports the same
`row_count`, (c) `/search` returns the round-tripped book, and (d)
`make test-isolation` is **3/3** — recovery must not weaken `book_id`
partition-key isolation (see
[CLAUDE.md → Tenant isolation](../CLAUDE.md#tenant-isolation-is-not-negotiable)).

## Troubleshooting

- **`compose network 'sermon_default' not found`** — the stack is down. `make up`.
- **`milvus-backup checksum mismatch`** — a corrupt/MITM'd download; delete
  `infra/backups/.tools/` and re-run, or bump the pin + sha in `lib.sh`.
- **restore finds the collection but `/search` returns nothing** — the
  collection is restored but not loaded into memory; run `load_collection`
  (step 6b) or hit `/readyz`, which loads it.
- **`pg_restore` errors about existing objects** — expected/benign with
  `--clean --if-exists` on a non-empty DB; the restore still converges. For a
  truly clean restore, nuke first.
- **old backups piling up** — they're root-owned (container writes);
  `sudo rm -rf infra/backups/<stamp>` to prune. Keep `latest` valid.
