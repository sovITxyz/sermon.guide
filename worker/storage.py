"""Object storage for raw uploaded originals (Phase 31).

Before this phase, originals lived only in volatile ``/tmp/sermon-uploads``
and MinHash dedup short-circuited second owners before any write — every
book ingested had a permanently unrecoverable original. This module is the
write-only persistence seam: ``ingest.py`` uploads each original to the
compose MinIO under ``originals/{book_id}/{sanitized-filename}`` and stores
that key in ``global_books.text_pointer`` (plumbed since Phase 7, never
filled until now).

## Scope fence (B1 / Phase 31)

Write-only. There is deliberately NO read endpoint and NO presigned-URL
surface until the full-fidelity reader tier ships — zero new tenant read
surface. Do not add reads here without re-running the tenant gates.

## Client choice

minio-py over boto3 (decision recorded in ``AGENTS.md``): ships
``py.typed`` so pyright strict needs no stub relaxations, pulls ~5 small
deps instead of botocore's ~80MB, and speaks plain S3 — the future R2/B2
swap is endpoint + credentials in ``StorageSettings``, nothing else.

## Failure posture

Storage failures raise :class:`OriginalsStorageError` and are NOT swallowed
— the caller (``ingest.py``) lets them fail the ingest loudly. Durability
is the point of this phase; log-and-continue would be silent data loss.

## Key hygiene

The filename component of the key is client-supplied and untrusted. It is
sanitized with the exact rules of ``api/uploads.py:_sanitize_filename``
(mirrored, not imported — worker/ must never import api/): path separators,
dot-dot, control characters, and anything outside ``[A-Za-z0-9._-]`` are
stripped or replaced, so the key can never escape the
``originals/{book_id}/`` prefix.
"""

from __future__ import annotations

import re
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from uuid import UUID

from minio import Minio
from minio.error import S3Error
from pydantic_settings import BaseSettings, SettingsConfigDict
from urllib3.exceptions import HTTPError


class StorageSettings(BaseSettings):
    """MinIO/S3 connection settings from ``SERMON_MINIO_*``.

    Reuses the root credentials + API port the compose stack already
    defines (``infra/docker-compose.yml``); ``host``/``secure``/
    ``originals_bucket`` are new, append-only documented in
    ``infra/.env.example``. ``host`` defaults to the host-side mapping
    (``localhost``), never the compose-internal ``minio:9000``. A future
    R2/B2 swap is host+port+secure+keys — no code change.
    """

    model_config = SettingsConfigDict(env_prefix="SERMON_MINIO_", extra="ignore")

    host: str = "localhost"
    port: int = 9000
    root_user: str = "minioadmin"
    root_password: str = "minioadmin"  # noqa: S105 — matches infra/.env local-dev default
    originals_bucket: str = "sermon-originals"
    secure: bool = False

    @property
    def endpoint(self) -> str:
        """``host:port`` form the minio client expects (no scheme)."""
        return f"{self.host}:{self.port}"


settings = StorageSettings()

#: Top-level key prefix for raw uploads; the only namespace this module writes.
ORIGINALS_PREFIX = "originals"

# Mirror of api/uploads.py:_FILENAME_SANITIZE — keep the two in sync by hand;
# worker/ must never import api/ (repo dep-direction rule).
_FILENAME_SANITIZE = re.compile(r"[^A-Za-z0-9._-]")

# Object-key segment cap. Filesystem-backed uploads are already ≤255 (ext4
# basename limit at API write time); this guards the bytes seam against a
# hostile multipart filename blowing S3's 1024-byte key ceiling.
_MAX_FILENAME_CHARS = 255


class OriginalsStorageError(RuntimeError):
    """An originals-bucket operation failed.

    Message names the operation and endpoint, never credentials. Callers
    in ``ingest.py`` let this propagate — a failed upload fails the
    ingest (posture recorded in ``AGENTS.md``).
    """


def sanitize_filename(raw: str | None) -> str:
    """Strip path components and unsafe characters; fall back to ``upload.bin``.

    Exact mirror of ``api/uploads.py:_sanitize_filename`` (see module
    docstring for why it is mirrored, not imported), plus a length cap for
    object-key safety. Backslashes normalize to forward slashes first so
    Windows-style paths lose their directory part on Linux too; leading
    dots are stripped so the segment can never be ``..``; the regex then
    collapses anything outside ``[A-Za-z0-9._-]`` to ``_``.
    """
    if not raw:
        return "upload.bin"
    base = Path(raw.replace("\\", "/")).name.lstrip(".")
    cleaned = _FILENAME_SANITIZE.sub("_", base)[:_MAX_FILENAME_CHARS]
    return cleaned or "upload.bin"


def original_key(book_id: UUID, filename: str | None) -> str:
    """Object key for a book's raw original: ``originals/{book_id}/{sanitized}``.

    *book_id* is repo-minted (never client-supplied) and *filename* is
    sanitized, so the key always stays under the book's own prefix.
    """
    return f"{ORIGINALS_PREFIX}/{book_id}/{sanitize_filename(filename)}"


@lru_cache(maxsize=1)
def storage_client() -> Minio:
    """Process-wide minio client (lazy; constructing it opens no connection)."""
    return Minio(
        settings.endpoint,
        access_key=settings.root_user,
        secret_key=settings.root_password,
        secure=settings.secure,
    )


def _fail_msg(op: str) -> str:
    """Failure message naming the operation + endpoint (never credentials)."""
    return f"originals storage: {op} failed against {settings.endpoint!r}"


def ensure_originals_bucket(client: Minio | None = None) -> None:
    """Create the originals bucket if missing. Idempotent and race-safe.

    Two workers can both observe the bucket missing and both call
    ``make_bucket``; the loser's ``BucketAlreadyOwnedByYou`` /
    ``BucketAlreadyExists`` is swallowed — same end state either way.
    """
    client = client if client is not None else storage_client()
    bucket = settings.originals_bucket
    try:
        if client.bucket_exists(bucket):
            return
        client.make_bucket(bucket)
    except S3Error as exc:
        if exc.code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            return
        msg = _fail_msg(f"ensure-bucket {bucket!r}")
        raise OriginalsStorageError(msg) from exc
    except (HTTPError, OSError) as exc:
        msg = _fail_msg(f"ensure-bucket {bucket!r}")
        raise OriginalsStorageError(msg) from exc


def put_original(
    *,
    book_id: UUID,
    filename: str | None,
    data: bytes | Path,
    client: Minio | None = None,
) -> str:
    """Upload a raw original; return its object key.

    *data* is either the original bytes or a path to the original file
    (streamed, not slurped). Ensures the bucket exists first. Raises
    :class:`OriginalsStorageError` on any storage failure — callers must
    not swallow it (fail-the-ingest posture).
    """
    client = client if client is not None else storage_client()
    ensure_originals_bucket(client)
    bucket = settings.originals_bucket
    key = original_key(book_id, filename)
    try:
        if isinstance(data, Path):
            client.fput_object(bucket, key, str(data))
        else:
            client.put_object(bucket, key, BytesIO(data), length=len(data))
    except (S3Error, HTTPError, OSError) as exc:
        msg = _fail_msg(f"put {key!r}")
        raise OriginalsStorageError(msg) from exc
    return key


def object_exists(key: str, client: Minio | None = None) -> bool:
    """True when *key* exists in the originals bucket.

    A missing object or missing bucket is ``False``; any other storage
    failure raises :class:`OriginalsStorageError`.
    """
    client = client if client is not None else storage_client()
    try:
        client.stat_object(settings.originals_bucket, key)
    except S3Error as exc:
        if exc.code in ("NoSuchKey", "NoSuchBucket"):
            return False
        msg = _fail_msg(f"stat {key!r}")
        raise OriginalsStorageError(msg) from exc
    except (HTTPError, OSError) as exc:
        msg = _fail_msg(f"stat {key!r}")
        raise OriginalsStorageError(msg) from exc
    return True
