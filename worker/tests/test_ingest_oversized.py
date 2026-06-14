"""Live regression: an oversized chunk no longer breaks the Milvus insert.

The bug (live): the SemanticSplitter sizes boundaries on shifts in MEANING,
not on size, so a large *homogeneous* section (a concordance, an index, a long
run of near-identical sentences) collapses into one chunk. When that chunk's
text exceeds Milvus's 65535-BYTE ``content_chunk`` VARCHAR cap, the insert is
rejected with a ``MilvusException`` and the ingest fails — which is what was
failing the corpus-seed drill and blocking the golden gate (a 355687-char
chunk from a seeded EPUB).

The fix (``chunking._cap_oversized_chunks``) sub-splits any over-cap chunk so
every stored ``content_chunk`` fits. This test drives a synthetic document
with a deliberately large homogeneous section through the REAL ingest path
into the live stack and asserts the insert SUCCEEDS and every stored
``content_chunk`` is within the byte cap.

Skips cleanly without (a) ``DEEPINFRA_API_KEY`` (the boundary + chunk
embeddings are remote — Phase 16b), (b) reachable Milvus, (c) reachable
Postgres, or (d) the NLTK WordNet corpus (the dedup signature needs it). The
operator runs the live suites; this module only needs to import/collect clean
where the gates are unmet.
"""

# Reaches for internal ingest helpers and the chunk-byte cap — the cheapest
# covered path for the regression. Mirrors test_ingest.py's relaxations.
# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnnecessaryComparison=false

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Iterator

import pytest

from chunking import _HARD_MAX_CHUNK_BYTES
from db import Chunk as ChunkRow
from db import GlobalBook, User, UserLibraryEntry, get_sync_session_factory
from db.settings import settings as db_settings
from ingest import ingest_markdown
from scripts.bootstrap_milvus import COLLECTION_NAME

# Deterministic identities so a crashed/re-run live drill converges on the same
# rows instead of leaving orphans. uuid5 over a fixed namespace + stable names.
_TEST_NAMESPACE = uuid.UUID("6f0d1c2e-9a4b-5c6d-8e7f-0a1b2c3d4e5f")
_TEST_USER_ID = uuid.uuid5(_TEST_NAMESPACE, "ingest-oversized-test-user")
_TEST_TITLE = "_test_oversized_chunk"
_TEST_FILENAME = "oversized.md"

# A single large HOMOGENEOUS section: one heading, then many near-identical
# sentences. Their pairwise embedding distance is tiny, so the SemanticSplitter
# does NOT break the run — pre-fix it emitted one chunk far over the 65535-byte
# cap. ~3000 copies of an ~80-byte sentence ≈ 240 KB, comfortably over the cap
# (and over a single chunk's window even after a few meaning-driven breaks).
_HOMOGENEOUS_SENTENCE = "The diligent reader returns once more to the very same quiet passage. "
OVERSIZED_MARKDOWN = "# Concordance\n\n" + (_HOMOGENEOUS_SENTENCE * 3000) + "\n"


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


def _delete_book_everywhere(book_id: uuid.UUID) -> None:
    """Drop the test book's rows from Milvus and Postgres, FK-safe.

    ``book_id`` is a UUID minted by this module (never client input), so the
    Milvus filter interpolation cannot be injected into. Order: user_library
    (FK to global_books is ON DELETE RESTRICT) → chunks → global_books → the
    synthetic user.
    """
    from pymilvus import MilvusClient
    from sqlalchemy import delete

    host, port = _milvus_host_port()
    client = MilvusClient(uri=f"http://{host}:{port}")
    if client.has_collection(collection_name=COLLECTION_NAME):
        client.delete(collection_name=COLLECTION_NAME, filter=f'book_id == "{book_id}"')
        client.flush(collection_name=COLLECTION_NAME)

    sf = get_sync_session_factory()
    with sf() as session, session.begin():
        session.execute(delete(UserLibraryEntry).where(UserLibraryEntry.book_id == book_id))
        session.execute(delete(ChunkRow).where(ChunkRow.book_id == book_id))
        session.execute(delete(GlobalBook).where(GlobalBook.book_id == book_id))
        session.execute(delete(User).where(User.user_id == _TEST_USER_ID))


@pytest.fixture
def oversized_clean() -> Iterator[None]:
    """Seed the synthetic user and scrub any prior run's rows; clean up after.

    Deterministic ids mean a re-run would otherwise dedup-hit a leftover book;
    the pre-scrub forces a true new-book ingest every run so the oversized
    insert path is actually exercised.
    """
    if not (_milvus_reachable() and _postgres_reachable()):
        yield
        return

    sf = get_sync_session_factory()
    # The book_id is content-derived by dedup, so we cannot pre-compute it;
    # scrub by title to clear any leftover from a prior run before this one.
    from sqlalchemy import delete, select

    with sf() as session, session.begin():
        leftovers = list(
            session.execute(
                select(GlobalBook.book_id).where(GlobalBook.title == _TEST_TITLE),
            ).scalars()
        )
    for book_id in leftovers:
        _delete_book_everywhere(book_id)

    with sf() as session, session.begin():
        session.execute(delete(User).where(User.user_id == _TEST_USER_ID))
        session.add(
            User(
                user_id=_TEST_USER_ID,
                email=f"{_TEST_USER_ID}@test.local",
                password_hash="bcrypt$test",  # noqa: S106 — test fixture only
            ),
        )

    created: list[uuid.UUID] = []
    try:
        yield
        # Discover what we created (book_id is dedup-decided) and clean it up.
        with sf() as session:
            created = list(
                session.execute(
                    select(GlobalBook.book_id).where(GlobalBook.title == _TEST_TITLE),
                ).scalars()
            )
    finally:
        for book_id in created:
            _delete_book_everywhere(book_id)
        # Drop the user even if nothing was created (e.g. an early failure).
        with sf() as session, session.begin():
            session.execute(delete(User).where(User.user_id == _TEST_USER_ID))


@pytest.mark.skipif(
    not _remote_embeddings_available(),
    reason="DEEPINFRA_API_KEY unset — remote embeddings unavailable",
)
@pytest.mark.skipif(
    not _wordnet_available(),
    reason="NLTK WordNet corpus not installed; run `nltk.download('wordnet')` to enable.",
)
def test_oversized_chunk_ingests_within_milvus_cap(
    oversized_clean: None,  # noqa: ARG001 — fixture used for setup/teardown
) -> None:
    """A large homogeneous section ingests cleanly — every content_chunk fits.

    Pre-fix this raised a ``MilvusException`` (content_chunk over the 65535-byte
    VARCHAR cap). The assertions: ingest SUCCEEDS, a sane number of vector rows
    landed, and EVERY stored ``content_chunk`` is within the hard byte cap.
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

    from dedup import Dedup

    # Fresh empty dedup index so this run is a true new-book ingest (the
    # fixture already scrubbed any leftover book of the same content).
    dedup_index = Dedup(session_factory=get_sync_session_factory())
    dedup_index._load_from([])  # noqa: SLF001 — documented test seam

    # The real ingest path: chunk → embed → Milvus insert. Pre-fix the insert
    # raised; the assertion is simply that it does not.
    result = ingest_markdown(
        markdown=OVERSIZED_MARKDOWN,
        filename=_TEST_FILENAME,
        user_id=_TEST_USER_ID,
        client=client,
        dedup_index=dedup_index,
        title=_TEST_TITLE,
    )

    assert not result.was_duplicate
    assert result.rows_inserted > 1, (
        "the oversized section must sub-split into several capped chunks; a "
        "count of 1 means the over-cap chunk was NOT split"
    )

    # Every stored content_chunk must be within Milvus's hard byte cap — the
    # invariant the fix exists to guarantee.
    rows = client.query(
        collection_name=COLLECTION_NAME,
        filter=f'book_id == "{result.book_id}"',
        output_fields=["content_chunk"],
        limit=result.rows_inserted + 10,
    )
    assert len(rows) == result.rows_inserted
    for row in rows:
        content = row["content_chunk"]
        assert isinstance(content, str)
        assert len(content.encode("utf-8")) <= _HARD_MAX_CHUNK_BYTES, (
            f"stored content_chunk is {len(content.encode('utf-8'))} bytes, over "
            f"the {_HARD_MAX_CHUNK_BYTES}-byte Milvus cap — the sub-split pass missed it"
        )
