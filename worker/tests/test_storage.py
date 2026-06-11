"""Tests for Phase 31 originals persistence (``storage.py`` + ingest wiring).

Three layers:

1. **Pure unit** — filename sanitization and object-key construction.
   No MinIO, no Postgres. Runs anywhere.
2. **Storage round-trip** — ``put_original`` / ``object_exists`` against
   the compose MinIO. Skips cleanly when MinIO is unreachable.
3. **Ingest wiring** — the dup-hit backfill (never-overwrite invariant)
   and the new-book pointer write, driven through the real
   ``ingest_markdown`` seam. Skip cleanly without reachable
   Postgres/MinIO (+ NLTK WordNet for the dedup path, + Milvus for the
   new-book path).

The hostile-filename cases mirror the Phase 31 verify checklist: a key
built from ``../``-laced input must stay under ``originals/{book_id}/``.
"""

# Tests reach for `_backfill_original` (the dup-hit recovery seam) and the
# documented `_load_from` dedup test seam; nltk/datasketch ship without
# stubs (same relaxations as test_ingest.py).
# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false

from __future__ import annotations

import socket
import uuid
from collections.abc import Iterator

import pytest

import dedup as dedup_mod
import storage
from db import GlobalBook, User, UserLibraryEntry, get_sync_session_factory
from db.settings import settings as db_settings
from ingest import _backfill_original, ingest_markdown
from storage import object_exists, original_key, put_original, sanitize_filename
from storage import settings as storage_settings

# Distinct from test_ingest's synthetic text so the two suites never
# collide at the MinHash gate when run against the same Postgres.
PHASE31_MARKDOWN = """\
# On Keeping Originals

The scribe copied the scroll and then burned his only source. Every copy
after that inherited his mistakes. A careful archive keeps the first
witness; recovery starts from what was actually uploaded.
"""


def _minio_reachable() -> bool:
    try:
        with socket.create_connection((storage_settings.host, storage_settings.port), timeout=1.0):
            return True
    except OSError:
        return False


def _postgres_reachable() -> bool:
    try:
        with socket.create_connection((db_settings.host, db_settings.port), timeout=1.0):
            return True
    except OSError:
        return False


def _milvus_reachable() -> bool:
    import os

    host = os.environ.get("SERMON_MILVUS_HOST", "localhost")
    port = int(os.environ.get("SERMON_MILVUS_PORT", "19530"))
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _wordnet_available() -> bool:
    try:
        import nltk

        nltk.data.find("corpora/wordnet")
    except (ImportError, LookupError):
        return False
    return True


# ---------------------------------------------------------------------------
# Layer 1 — pure unit: sanitization + key construction
# ---------------------------------------------------------------------------


def test_sanitize_strips_path_traversal() -> None:
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("/etc/shadow") == "shadow"
    assert sanitize_filename("a/../../b/c.epub") == "c.epub"


def test_sanitize_normalizes_windows_separators() -> None:
    assert sanitize_filename("..\\..\\evil.epub") == "evil.epub"
    assert sanitize_filename("C:\\Users\\x\\book.pdf") == "book.pdf"


def test_sanitize_strips_leading_dots() -> None:
    assert sanitize_filename("...hidden.epub") == "hidden.epub"
    assert sanitize_filename(".bashrc") == "bashrc"


def test_sanitize_replaces_control_and_special_chars() -> None:
    assert sanitize_filename("my book\x00\x1f!.epub") == "my_book___.epub"
    assert sanitize_filename("née könig.pdf") == "n_e_k_nig.pdf"


def test_sanitize_falls_back_on_empty_inputs() -> None:
    assert sanitize_filename(None) == "upload.bin"
    assert sanitize_filename("") == "upload.bin"
    assert sanitize_filename("....") == "upload.bin"
    assert sanitize_filename("///") == "upload.bin"


def test_sanitize_caps_length_for_key_safety() -> None:
    assert len(sanitize_filename("a" * 5000 + ".epub")) == 255


def test_original_key_stays_under_book_prefix_for_hostile_names() -> None:
    book_id = uuid.uuid4()
    for hostile in ("../../etc/passwd", "..\\..\\x.epub", "a/b/../c.pdf", None, "weird name?.md"):
        key = original_key(book_id, hostile)
        parts = key.split("/")
        assert parts[0] == storage.ORIGINALS_PREFIX
        assert parts[1] == str(book_id)
        assert len(parts) == 3, f"key escaped the book prefix: {key!r}"
        assert ".." not in parts[2]


def test_original_key_plain_filename() -> None:
    book_id = uuid.uuid4()
    assert original_key(book_id, "book.epub") == f"originals/{book_id}/book.epub"


# ---------------------------------------------------------------------------
# Shared live-test helpers
# ---------------------------------------------------------------------------


def _purge_originals(book_id: uuid.UUID) -> None:
    """Remove every object under the book's originals prefix (teardown)."""
    client = storage.storage_client()
    bucket = storage_settings.originals_bucket
    if not client.bucket_exists(bucket):
        return
    prefix = f"{storage.ORIGINALS_PREFIX}/{book_id}/"
    for obj in client.list_objects(bucket, prefix=prefix, recursive=True):
        name = obj.object_name
        if name is not None:
            client.remove_object(bucket, name)


def _objects_under(book_id: uuid.UUID) -> list[str]:
    client = storage.storage_client()
    bucket = storage_settings.originals_bucket
    if not client.bucket_exists(bucket):
        return []
    prefix = f"{storage.ORIGINALS_PREFIX}/{book_id}/"
    return [
        obj.object_name
        for obj in client.list_objects(bucket, prefix=prefix, recursive=True)
        if obj.object_name is not None
    ]


def _make_user() -> uuid.UUID:
    """Seed a row in ``users`` and return its ``user_id``."""
    user_id = uuid.uuid4()
    sf = get_sync_session_factory()
    with sf() as session, session.begin():
        session.add(
            User(
                user_id=user_id,
                email=f"{user_id}@test.local",
                password_hash="bcrypt$test",  # noqa: S106 — test fixture only
            ),
        )
    return user_id


def _seed_book(*, text_pointer: str | None, signature_bytes: bytes = b"\x00") -> uuid.UUID:
    """Seed a bare ``global_books`` row; returns its ``book_id``."""
    book_id = uuid.uuid4()
    sf = get_sync_session_factory()
    with sf() as session, session.begin():
        session.add(
            GlobalBook(
                book_id=book_id,
                title="_test_phase31_seed",
                author=None,
                minhash_signature=signature_bytes,
                text_pointer=text_pointer,
            ),
        )
    return book_id


def _book_pointer(book_id: uuid.UUID) -> str | None:
    from sqlalchemy import select

    sf = get_sync_session_factory()
    with sf() as session:
        return session.execute(
            select(GlobalBook.text_pointer).where(GlobalBook.book_id == book_id),
        ).scalar_one()


def _cleanup_rows(*, user_ids: list[uuid.UUID], book_ids: list[uuid.UUID]) -> None:
    """Delete test rows in FK-safe order: user_library → books → users."""
    from sqlalchemy import delete

    sf = get_sync_session_factory()
    with sf() as session, session.begin():
        if user_ids:
            session.execute(
                delete(UserLibraryEntry).where(UserLibraryEntry.user_id.in_(user_ids)),
            )
        if book_ids:
            session.execute(
                delete(UserLibraryEntry).where(UserLibraryEntry.book_id.in_(book_ids)),
            )
            session.execute(delete(GlobalBook).where(GlobalBook.book_id.in_(book_ids)))
        if user_ids:
            session.execute(delete(User).where(User.user_id.in_(user_ids)))


@pytest.fixture
def tracked_books() -> Iterator[list[uuid.UUID]]:
    """Collects book_ids; purges their originals objects on teardown."""
    book_ids: list[uuid.UUID] = []
    yield book_ids
    if _minio_reachable():
        for book_id in book_ids:
            _purge_originals(book_id)


# ---------------------------------------------------------------------------
# Layer 2 — storage round-trip against the compose MinIO
# ---------------------------------------------------------------------------


def test_put_original_roundtrip(tracked_books: list[uuid.UUID]) -> None:
    if not _minio_reachable():
        pytest.skip(f"MinIO unreachable at {storage_settings.endpoint}; run `make up`.")

    book_id = uuid.uuid4()
    tracked_books.append(book_id)
    payload = b"%PDF-1.4 tiny phase31 fixture\n"

    key = put_original(book_id=book_id, filename="tiny book.pdf", data=payload)
    assert key == f"originals/{book_id}/tiny_book.pdf"
    assert object_exists(key)

    client = storage.storage_client()
    response = client.get_object(storage_settings.originals_bucket, key)
    try:
        assert response.read() == payload
    finally:
        response.close()
        response.release_conn()

    # Idempotent bucket creation: a second upload must not trip on the
    # already-existing bucket.
    key2 = put_original(book_id=book_id, filename="tiny book.pdf", data=payload)
    assert key2 == key


def test_object_exists_false_for_missing_key() -> None:
    if not _minio_reachable():
        pytest.skip(f"MinIO unreachable at {storage_settings.endpoint}; run `make up`.")
    assert not object_exists(f"originals/{uuid.uuid4()}/nope.epub")


# ---------------------------------------------------------------------------
# Layer 3 — ingest wiring: backfill + never-overwrite + new-book pointer
# ---------------------------------------------------------------------------


def test_backfill_fills_null_pointer_exactly_once(tracked_books: list[uuid.UUID]) -> None:
    """NULL pointer → backfilled; set pointer → untouched, no second object."""
    if not _minio_reachable():
        pytest.skip(f"MinIO unreachable at {storage_settings.endpoint}; run `make up`.")
    if not _postgres_reachable():
        pytest.skip(
            f"Postgres unreachable at {db_settings.host}:{db_settings.port}; run `make up`."
        )

    book_id = _seed_book(text_pointer=None)
    tracked_books.append(book_id)
    try:
        _backfill_original(book_id=book_id, filename="first owner.epub", original=b"raw-bytes")
        first_key = f"originals/{book_id}/first_owner.epub"
        assert _book_pointer(book_id) == first_key
        assert object_exists(first_key)

        # Second dup-hit with a different filename: pointer already set →
        # no overwrite, no second object, zero new storage writes.
        _backfill_original(book_id=book_id, filename="second owner.epub", original=b"raw-bytes")
        assert _book_pointer(book_id) == first_key
        assert _objects_under(book_id) == [first_key]
    finally:
        _cleanup_rows(user_ids=[], book_ids=[book_id])


def test_backfill_noops_when_pointer_already_set(tracked_books: list[uuid.UUID]) -> None:
    if not _minio_reachable():
        pytest.skip(f"MinIO unreachable at {storage_settings.endpoint}; run `make up`.")
    if not _postgres_reachable():
        pytest.skip(
            f"Postgres unreachable at {db_settings.host}:{db_settings.port}; run `make up`."
        )

    sentinel = "originals/preexisting/key.epub"
    book_id = _seed_book(text_pointer=sentinel)
    tracked_books.append(book_id)
    try:
        _backfill_original(book_id=book_id, filename="late copy.epub", original=b"bytes")
        assert _book_pointer(book_id) == sentinel
        assert _objects_under(book_id) == []
    finally:
        _cleanup_rows(user_ids=[], book_ids=[book_id])


def test_backfill_noops_for_missing_book_row() -> None:
    if not _minio_reachable():
        pytest.skip(f"MinIO unreachable at {storage_settings.endpoint}; run `make up`.")
    if not _postgres_reachable():
        pytest.skip(
            f"Postgres unreachable at {db_settings.host}:{db_settings.port}; run `make up`."
        )

    ghost = uuid.uuid4()
    _backfill_original(book_id=ghost, filename="ghost.epub", original=b"bytes")
    assert _objects_under(ghost) == []


@pytest.mark.skipif(
    not _wordnet_available(),
    reason="NLTK WordNet corpus not installed; run `nltk.download('wordnet')` to enable.",
)
def test_dup_hit_backfills_through_ingest_markdown(tracked_books: list[uuid.UUID]) -> None:
    """Second owner of a pre-Phase-31 book recovers its original.

    Drives the real ``ingest_markdown`` dup path: a preloaded dedup index
    short-circuits before Milvus/embeddings, so this needs only
    Postgres + MinIO (+ WordNet for the MinHash signature).
    """
    if not _minio_reachable():
        pytest.skip(f"MinIO unreachable at {storage_settings.endpoint}; run `make up`.")
    if not _postgres_reachable():
        pytest.skip(
            f"Postgres unreachable at {db_settings.host}:{db_settings.port}; run `make up`."
        )

    sig = dedup_mod.signature(PHASE31_MARKDOWN)
    book_id = _seed_book(text_pointer=None, signature_bytes=dedup_mod.serialize(sig))
    tracked_books.append(book_id)
    index = dedup_mod.Dedup(session_factory=get_sync_session_factory())
    index._load_from([])  # noqa: SLF001 — documented test seam
    index.add(book_id, sig)

    user_id = _make_user()
    try:
        result = ingest_markdown(
            markdown=PHASE31_MARKDOWN,
            filename="recovered copy.epub",
            user_id=user_id,
            dedup_index=index,
            title="_test_phase31_dup",
            original=b"the original epub bytes",
        )
        assert result.was_duplicate
        assert result.book_id == book_id
        assert result.rows_inserted == 0

        expected_key = f"originals/{book_id}/recovered_copy.epub"
        assert _book_pointer(book_id) == expected_key
        assert object_exists(expected_key)
    finally:
        _cleanup_rows(user_ids=[user_id], book_ids=[book_id])


def test_new_book_persists_original_and_pointer(tracked_books: list[uuid.UUID]) -> None:
    """New-book path: object lands under originals/{book_id}/ AND the
    pointer is committed with the book row.

    Whitespace-only markdown yields zero chunks, so the path exercises
    the upload + ``global_books`` write without remote embeddings; a
    live Milvus is still required because ``ingest_markdown``
    constructs its client before chunking.
    """
    if not _minio_reachable():
        pytest.skip(f"MinIO unreachable at {storage_settings.endpoint}; run `make up`.")
    if not _postgres_reachable():
        pytest.skip(
            f"Postgres unreachable at {db_settings.host}:{db_settings.port}; run `make up`."
        )
    if not _milvus_reachable():
        pytest.skip("Milvus unreachable; run `make up`.")

    index = dedup_mod.Dedup(session_factory=get_sync_session_factory())
    index._load_from([])  # noqa: SLF001 — documented test seam

    user_id = _make_user()
    book_id: uuid.UUID | None = None
    try:
        result = ingest_markdown(
            markdown="   \n",
            filename="../../etc/raw book.epub",
            user_id=user_id,
            dedup_index=index,
            title="_test_phase31_new",
            original=b"fresh original bytes",
        )
        book_id = result.book_id
        tracked_books.append(book_id)
        assert not result.was_duplicate
        assert result.rows_inserted == 0  # whitespace markdown → no chunks

        expected_key = f"originals/{book_id}/raw_book.epub"
        assert _book_pointer(book_id) == expected_key
        assert object_exists(expected_key)
        assert _objects_under(book_id) == [expected_key]
    finally:
        _cleanup_rows(user_ids=[user_id], book_ids=[book_id] if book_id else [])
