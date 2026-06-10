"""Tests for the dedup-aware single-book ingest pipeline (Phase 8).

Two layers:

1. **Pure unit** — ``_build_rows`` against synthetic chunks + a
   deterministic ndarray. No model load, no Milvus, no Postgres. Runs in
   CI.
2. **End-to-end** — drives ``ingest_markdown`` with a tiny synthetic
   markdown string, two simulated tenants, and the full dedup gate. Real
   remote BGE-Large (Phase 16b), real Milvus, real Postgres. Skips
   cleanly without (a) DEEPINFRA_API_KEY, (b) reachable Milvus, (c)
   reachable Postgres, or (d) NLTK WordNet corpus.

The e2e covers the Phase 8 verify checklist: ingest under tenant_a (new
book, vectors created), ingest the same content under tenant_b (no new
vectors, user_library pointer only).
"""

# Tests reach for `_build_rows` — a small internal helper that is the
# cheapest covered path for the metadata-shape assertions.
# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnnecessaryComparison=false

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Iterator

import numpy as np
import pytest

from chunking import Chunk
from db import GlobalBook, User, UserLibraryEntry, get_sync_session_factory
from db.settings import settings as db_settings
from embedding import EMBED_DIM
from ingest import _build_rows, ingest_markdown
from scripts.bootstrap_milvus import COLLECTION_NAME

# Five short distinct sentences across two ATX headings. SemanticSplitter
# can emit anywhere from 1 to a few chunks here; the e2e test only asserts
# `rows_inserted > 0` and per-row metadata shape, not exact chunk count.
SYNTHETIC_MARKDOWN = """\
# Introduction

The morning light fell across the open page. The reader paused to consider its meaning.

# Chapter One

A bell rang in the distance and the village stirred. Smoke rose from the cottages.
A child laughed somewhere down the lane.
"""


def _remote_embeddings_available() -> bool:
    """Phase 16b: chunk + boundary embeddings are remote calls now."""
    return bool(os.environ.get("DEEPINFRA_API_KEY"))


def _milvus_host_port() -> tuple[str, int]:
    host = os.environ.get("SERMON_MILVUS_HOST", "localhost")
    port = int(os.environ.get("SERMON_MILVUS_PORT", "19530"))
    return host, port


def _milvus_reachable() -> bool:
    host, port = _milvus_host_port()
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _postgres_reachable() -> bool:
    try:
        with socket.create_connection((db_settings.host, db_settings.port), timeout=1.0):
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


def test_build_rows_pairs_chunks_with_embeddings() -> None:
    chunks = [
        Chunk(text="alpha sentence.", start_idx=0, end_idx=15, parent_section="A"),
        Chunk(text="beta sentence.", start_idx=16, end_idx=30, parent_section="A"),
        Chunk(text="gamma sentence.", start_idx=31, end_idx=46, parent_section=None),
    ]
    embeddings = np.arange(len(chunks) * EMBED_DIM, dtype=np.float32).reshape(
        len(chunks), EMBED_DIM
    )
    rows = _build_rows(
        filename="book.epub",
        chunks=chunks,
        embeddings=embeddings,
        book_id="b_test",
    )

    assert len(rows) == 3
    for i, row in enumerate(rows):
        assert row["book_id"] == "b_test"
        assert row["content_chunk"] == chunks[i].text
        meta = row["metadata"]
        assert meta["filename"] == "book.epub"
        assert meta["chunk_index"] == i
        assert meta["parent_section"] == chunks[i].parent_section
        assert isinstance(row["vector"], list)
        assert len(row["vector"]) == EMBED_DIM


def test_build_rows_rejects_length_mismatch() -> None:
    chunks = [Chunk(text="solo.", start_idx=0, end_idx=5, parent_section=None)]
    embeddings = np.zeros((2, EMBED_DIM), dtype=np.float32)
    with pytest.raises(ValueError, match="length mismatch"):
        _build_rows(filename="x.epub", chunks=chunks, embeddings=embeddings, book_id="b")


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


def _cleanup_users_and_orphan_books(user_ids: list[uuid.UUID]) -> None:
    """Drop every row this test created, in FK-safe order.

    A single transaction deletes all the listed users' ``user_library``
    rows first, then any ``global_books`` row that is no longer
    referenced (a deduped book has multiple owners; we can only delete
    the row after every owner is gone), then the users themselves.
    """
    from sqlalchemy import delete, select

    sf = get_sync_session_factory()
    with sf() as session, session.begin():
        candidate_books = list(
            session.execute(
                select(UserLibraryEntry.book_id)
                .where(UserLibraryEntry.user_id.in_(user_ids))
                .distinct(),
            ).scalars()
        )
        session.execute(
            delete(UserLibraryEntry).where(UserLibraryEntry.user_id.in_(user_ids)),
        )
        for book_id in candidate_books:
            still_owned = session.execute(
                select(UserLibraryEntry.entry_id).where(UserLibraryEntry.book_id == book_id),
            ).first()
            if still_owned is None:
                session.execute(delete(GlobalBook).where(GlobalBook.book_id == book_id))
        session.execute(delete(User).where(User.user_id.in_(user_ids)))


@pytest.fixture
def milvus_clean_test_books() -> Iterator[None]:
    """Drop ``_test_phase8_*`` Milvus rows before and after the test."""
    if not _milvus_reachable():
        yield
        return
    from pymilvus import MilvusClient

    host, port = _milvus_host_port()
    client = MilvusClient(uri=f"http://{host}:{port}")
    if client.has_collection(collection_name=COLLECTION_NAME):
        client.delete(
            collection_name=COLLECTION_NAME,
            filter='book_id like "_test_phase8_%"',
        )
        client.flush(collection_name=COLLECTION_NAME)
    yield
    if client.has_collection(collection_name=COLLECTION_NAME):
        client.delete(
            collection_name=COLLECTION_NAME,
            filter='book_id like "_test_phase8_%"',
        )
        client.flush(collection_name=COLLECTION_NAME)


@pytest.mark.skipif(
    not _remote_embeddings_available(),
    reason="DEEPINFRA_API_KEY unset — remote embeddings unavailable",
)
@pytest.mark.skipif(
    not _wordnet_available(),
    reason="NLTK WordNet corpus not installed; run `nltk.download('wordnet')` to enable.",
)
def test_dedup_roundtrip_across_two_tenants(
    milvus_clean_test_books: None,  # noqa: ARG001 — fixture used for setup/teardown
) -> None:
    """Phase 8 verify path: same content under two tenants → shared vectors.

    If this fails, either the dedup gate is not catching exact-content
    re-uploads (would re-embed and double storage) or the user_library
    pointer is not landing under the second tenant (the dedup invariant
    in ARCHITECTURE.md §4 is broken).
    """
    if not _milvus_reachable():
        host, port = _milvus_host_port()
        pytest.skip(f"Milvus unreachable at {host}:{port}; run `make up`.")
    if not _postgres_reachable():
        pytest.skip(
            f"Postgres unreachable at {db_settings.host}:{db_settings.port}; run `make up`."
        )

    from pymilvus import MilvusClient

    host, port = _milvus_host_port()
    client = MilvusClient(uri=f"http://{host}:{port}")
    if not client.has_collection(collection_name=COLLECTION_NAME):
        pytest.skip(f"Collection '{COLLECTION_NAME}' missing — run `make bootstrap-milvus`.")

    # Fresh Dedup so the test's seeded books don't bleed in from prior
    # runs / production data. Pre-load it with no rows.
    from dedup import Dedup

    dedup_index = Dedup(session_factory=get_sync_session_factory())
    dedup_index._load_from([])  # noqa: SLF001 — documented test seam

    user_a = _make_user()
    user_b = _make_user()
    try:
        # First ingest: new book, vectors created.
        result_a = ingest_markdown(
            markdown=SYNTHETIC_MARKDOWN,
            filename="synthetic.md",
            user_id=user_a,
            client=client,
            dedup_index=dedup_index,
            title="_test_phase8_synthetic",
        )
        assert not result_a.was_duplicate
        assert result_a.rows_inserted > 0

        # Mark the Milvus rows with the test prefix so the fixture
        # teardown can clean them up. The Phase-8 ingest writes book_id
        # as the UUID string, which doesn't carry our prefix — replay
        # the metadata-prefix scrub the fixture relies on by querying
        # back and deleting alongside; in practice we tag via title
        # only and let `_test_phase8_*` UUID-prefixed cleanup handle
        # nothing. Cleanup uses the actual book_id below.

        # Verify Milvus rows exist for the freshly-created book_id.
        rows = client.query(
            collection_name=COLLECTION_NAME,
            filter=f'book_id == "{result_a.book_id}"',
            output_fields=["book_id"],
            limit=result_a.rows_inserted + 10,
        )
        assert len(rows) == result_a.rows_inserted

        # Second ingest, same user, same content: dedup catches it.
        result_a2 = ingest_markdown(
            markdown=SYNTHETIC_MARKDOWN,
            filename="synthetic.md",
            user_id=user_a,
            client=client,
            dedup_index=dedup_index,
            title="_test_phase8_synthetic",
        )
        assert result_a2.was_duplicate
        assert result_a2.rows_inserted == 0
        assert result_a2.book_id == result_a.book_id

        # Third ingest: different tenant, same content. Dedup must still
        # short-circuit and the second user's library row must point at
        # the same global_books row — the Phase-0 dedup-vs-isolation
        # decision (§7.1) hinges on this.
        result_b = ingest_markdown(
            markdown=SYNTHETIC_MARKDOWN,
            filename="synthetic.md",
            user_id=user_b,
            client=client,
            dedup_index=dedup_index,
            title="_test_phase8_synthetic",
        )
        assert result_b.was_duplicate
        assert result_b.rows_inserted == 0
        assert result_b.book_id == result_a.book_id

        # Confirm both tenants have a user_library row pointing at the
        # same book_id (the dedup invariant: one set of vectors, many
        # owners).
        sf = get_sync_session_factory()
        from sqlalchemy import select

        with sf() as session:
            entries = list(
                session.execute(
                    select(UserLibraryEntry.user_id, UserLibraryEntry.book_id).where(
                        UserLibraryEntry.book_id == result_a.book_id
                    ),
                ).all()
            )
        owners = {row[0] for row in entries}
        assert user_a in owners
        assert user_b in owners

        # Phase-0 §7.1 isolation check: an API search for user_b
        # filtered to `book_id IN (user_b's library)` MUST return the
        # shared book's vectors. Simulate the filter the API would
        # build.
        b_books = [row[1] for row in entries if row[0] == user_b]
        assert b_books == [result_a.book_id]
        quoted = ", ".join(f'"{b}"' for b in b_books)
        results = client.search(
            collection_name=COLLECTION_NAME,
            data=[[0.0] * EMBED_DIM],
            filter=f"book_id in [{quoted}]",
            limit=result_a.rows_inserted,
            output_fields=["book_id"],
        )
        hit_book_ids = {hit["entity"]["book_id"] for hit in results[0]}
        assert hit_book_ids == {str(result_a.book_id)}, (
            "Tenant B filtered search did not return the shared deduped "
            "book's vectors — Phase-0 §7.1 isolation decision is broken. "
            "STOP and revisit the Open Question."
        )

        # Targeted Milvus cleanup for the just-created book.
        client.delete(
            collection_name=COLLECTION_NAME,
            filter=f'book_id == "{result_a.book_id}"',
        )
        client.flush(collection_name=COLLECTION_NAME)
    finally:
        _cleanup_users_and_orphan_books([user_a, user_b])
