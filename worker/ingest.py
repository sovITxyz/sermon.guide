"""Dedup-aware single-book ingest: file → maybe chunks → maybe vectors → DB rows.

Phase 8 pipeline (ARCHITECTURE.md §5 upload path):

    detect (libmagic)
        → extract (EbookLib+pandoc or pymupdf4llm)
        → signature (MinHash, 5-shingles, lemmatized)
        → dedup lookup
              ├── duplicate? → insert user_library row;
              │                backfill original upload when the
              │                existing text_pointer is NULL
              │                (skip chunking + embedding)
              └── new?       → upload original → chunk → embed →
                               insert vectors; insert global_books
                               (text_pointer = originals key) +
                               user_library; LSH.add to the index.

## Originals persistence (Phase 31)

The raw upload is persisted to the originals bucket (``storage.py``)
*before* the ``global_books`` transaction, so a stored ``text_pointer``
never dangles; a crash between upload and commit leaves an orphan
object — the same accepted posture as the documented Milvus
orphan-vector window (``celery_app.py`` docstring). Storage failures
raise and **fail the ingest loudly** on both paths (posture recorded in
``AGENTS.md``); on the dup-hit path the ``user_library`` upsert lands
first, so a failed backfill still converges on retry while the pointer
stays NULL for the next attempt. Backfill never overwrites a set
pointer and never writes a second object for the same book
(``UPDATE … WHERE text_pointer IS NULL`` — race-safe, idempotent).

## Tenant scoping

Vectors are **shared globally per book** — there is no ``tenant_id`` on
the row (ARCHITECTURE.md §3 + §7.1). Tenant ownership lives in
``user_library``; every successful ingest writes that row, whether the
book is new or deduplicated. ``user_id`` is never placed in vector
metadata — that would defeat the dedup story (a single deduped book
serves every owner). API-layer search resolves the JWT user's
``book_id`` set from ``user_library`` and passes it as the Milvus
filter; see ``.claude/agents/tenant-auditor.md``.

## Idempotency

The unique constraint ``uq_user_library_user_book`` makes the
``user_library`` insert idempotent per ``(user_id, book_id)``: re-ingest
of the exact same book by the same user uses ``ON CONFLICT DO NOTHING``
and the second call is a no-op for the library row. Dedup short-circuits
chunking before that point on every re-upload of the same content, so
the second call costs one ``GlobalBook`` lookup + one ``user_library``
upsert.

CLI (run from ``worker/``):

    uv run python -m ingest path/to/book.epub --user-id <uuid>
"""

# pymilvus 2.6 ships without `py.typed`; datasketch is also stub-less. Same
# relaxations as bootstrap_milvus.py / chunking.py.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnnecessaryComparison=false

from __future__ import annotations

import argparse
import sys
import uuid as uuidlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np
from pymilvus import MilvusClient
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

import dedup
import storage
from chunking import Chunk, chunk
from db import Chunk as ChunkRow
from db import GlobalBook, UserLibraryEntry, get_sync_session_factory
from dedup import Dedup
from embedding import embed
from extractors import extract
from scripts.bootstrap_milvus import COLLECTION_NAME, make_client


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of one dedup-aware ingest run.

    ``rows_inserted`` is the Milvus vector count; zero when the book was
    a duplicate (``was_duplicate=True``) or when chunking produced no
    chunks. The ``book_id`` is the ``global_books`` PK either freshly
    minted or matched from an existing row.
    """

    book_id: UUID
    was_duplicate: bool
    rows_inserted: int


def _build_rows(
    *,
    filename: str,
    chunks: list[Chunk],
    embeddings: np.ndarray,
    book_id: str,
) -> list[dict[str, Any]]:
    """Zip *chunks* and *embeddings* into Milvus row dicts.

    Metadata follows ARCHITECTURE.md §3:
    ``{filename, chunk_index, parent_section}``. ``page`` is omitted —
    the Phase 5 chunker doesn't carry page anchors and the field is
    documented as optional.
    """
    if embeddings.shape[0] != len(chunks):
        msg = (
            f"chunks/embeddings length mismatch: {len(chunks)} chunks vs "
            f"{embeddings.shape[0]} embeddings."
        )
        raise ValueError(msg)
    vectors = embeddings.tolist()
    return [
        {
            "vector": vectors[i],
            "book_id": book_id,
            "content_chunk": c.text,
            "metadata": {
                "filename": filename,
                "chunk_index": i,
                "parent_section": c.parent_section,
            },
        }
        for i, c in enumerate(chunks)
    ]


def _insert_book_with_chunks(
    *,
    book_id: UUID,
    title: str,
    author: str | None,
    signature_bytes: bytes,
    text_pointer: str | None,
    chunks: list[Chunk],
    filename: str,
) -> None:
    """Insert ``global_books`` + ``chunks`` rows for a new book in one txn.

    Phase 12 (ADR 0004) introduced the ``chunks`` table for the BM25 arm
    of hybrid retrieval. Both writes share a transaction: if either
    fails, neither lands. Splitting them would create a worse failure
    mode than the existing Milvus/Postgres orphan-vector window — a
    ``global_books`` row without chunks would survive dedup but be
    invisible to BM25 forever, since re-ingest of the same content would
    short-circuit at the MinHash gate.

    Sync session bridge so the worker can write without spinning up an
    event loop. See ``db/session.py:get_sync_engine`` for the rationale.

    The parent ``GlobalBook`` is flushed *before* the child ``chunks`` rows so
    the ``fk_chunks_book_id`` foreign key is satisfied. SQLAlchemy's unit of
    work does not order these two inserts by the raw FK on its own — there is
    no ORM ``relationship()`` linking them — so without the explicit flush the
    emit order depends on mapper-registration order, which is exactly the kind
    of latent ordering bug a model-list edit can silently flip. Both writes
    still share one transaction: a failure in either rolls back both.
    """
    sf = get_sync_session_factory()
    with sf() as session, session.begin():
        session.add(
            GlobalBook(
                book_id=book_id,
                title=title,
                author=author,
                minhash_signature=signature_bytes,
                text_pointer=text_pointer,
            ),
        )
        if chunks:
            session.flush()  # parent INSERT before the child chunks — see docstring
            session.add_all(
                [
                    ChunkRow(
                        book_id=book_id,
                        chunk_index=i,
                        content=c.text,
                        parent_section=c.parent_section,
                        filename=filename,
                    )
                    for i, c in enumerate(chunks)
                ],
            )


def _backfill_original(*, book_id: UUID, filename: str, original: bytes | Path) -> None:
    """Persist *original* for an existing book whose ``text_pointer`` is NULL.

    The dup-hit recovery path (Phase 31): every book ingested before
    originals persistence landed has a NULL ``text_pointer`` and an
    unrecoverable original — a second owner re-uploading identical
    content is the only chance to capture the bytes. Already-set pointer
    (or missing row) → no-op, zero storage calls, never a duplicate
    object. The ``UPDATE … WHERE text_pointer IS NULL`` predicate makes
    the pointer write race-safe and idempotent: a concurrent backfill
    loses the update harmlessly (its object becomes an orphan under the
    same accepted posture as orphan vectors).

    Storage failures propagate (fail-the-ingest posture, AGENTS.md);
    the caller upserts ``user_library`` *before* invoking this, so a
    loud failure here still leaves the user's library converged and the
    NULL pointer retryable on the next dup-hit.
    """
    sf = get_sync_session_factory()
    with sf() as session:
        row = session.execute(
            select(GlobalBook.text_pointer).where(GlobalBook.book_id == book_id),
        ).one_or_none()
    if row is None or row[0] is not None:
        return
    key = storage.put_original(book_id=book_id, filename=filename, data=original)
    with sf() as session, session.begin():
        session.execute(
            update(GlobalBook)
            .where(GlobalBook.book_id == book_id, GlobalBook.text_pointer.is_(None))
            .values(text_pointer=key),
        )


def _upsert_user_library(*, user_id: UUID, book_id: UUID) -> None:
    """Ensure a ``user_library`` row binding *user_id* → *book_id* exists.

    Uses ``INSERT … ON CONFLICT DO NOTHING`` against the
    ``uq_user_library_user_book`` constraint so duplicate ingests under
    the same user are idempotent (the row is the *fact* that the user
    owns the book; multiple inserts shouldn't create multiple facts).
    """
    sf = get_sync_session_factory()
    with sf() as session, session.begin():
        stmt = (
            pg_insert(UserLibraryEntry)
            .values(user_id=user_id, book_id=book_id)
            .on_conflict_do_nothing(constraint="uq_user_library_user_book")
        )
        session.execute(stmt)


def ingest_markdown(
    *,
    markdown: str,
    filename: str,
    user_id: UUID,
    client: MilvusClient | None = None,
    dedup_index: Dedup | None = None,
    title: str | None = None,
    author: str | None = None,
    original: bytes | Path | None = None,
) -> IngestResult:
    """Dedup-aware ingest from already-extracted markdown.

    Split out from ``ingest()`` so tests can drive the chunk → embed →
    insert seam with a tiny synthetic document without paying the ~10
    min cost of semantic chunking on a novel-sized EPUB.

    *title* defaults to *filename* when missing; the dedup decision is
    based on content, not metadata.

    *client* and *dedup_index* default to the process-wide singletons
    when not supplied; pass them explicitly from tests that want to
    isolate state.

    *original* is the raw upload — bytes or a path to the on-disk file
    (Phase 31). New books upload it to ``originals/{book_id}/…`` and
    record the key in ``global_books.text_pointer``; dup-hits backfill
    the existing row's NULL pointer. ``None`` (the synthetic-markdown
    test seam) skips persistence entirely.
    """
    title = title if title is not None else filename
    sig = dedup.signature(markdown)
    index = dedup_index if dedup_index is not None else dedup.dedup_index()

    existing = index.find_duplicate(sig)
    if existing is not None:
        # Library row first: a loud backfill failure below still leaves
        # the user's ownership converged (retry is a no-op upsert).
        _upsert_user_library(user_id=user_id, book_id=existing)
        if original is not None:
            _backfill_original(book_id=existing, filename=filename, original=original)
        return IngestResult(
            book_id=existing,
            was_duplicate=True,
            rows_inserted=0,
        )

    client = client if client is not None else make_client()
    book_id = uuidlib.uuid4()
    # Persist the original before any chunk/embed/insert work: fail fast
    # while nothing has been written, and never commit a text_pointer
    # whose object doesn't exist (crash after upload → orphan object,
    # the accepted posture — see module docstring).
    text_pointer = (
        storage.put_original(book_id=book_id, filename=filename, data=original)
        if original is not None
        else None
    )
    chunks = chunk(markdown)
    rows_inserted = 0
    if chunks:
        embeddings = embed([c.text for c in chunks])
        rows = _build_rows(
            filename=filename,
            chunks=chunks,
            embeddings=embeddings,
            book_id=str(book_id),
        )
        client.insert(collection_name=COLLECTION_NAME, data=rows)
        client.flush(collection_name=COLLECTION_NAME)
        client.load_collection(collection_name=COLLECTION_NAME)
        rows_inserted = len(rows)

    _insert_book_with_chunks(
        book_id=book_id,
        title=title,
        author=author,
        signature_bytes=dedup.serialize(sig),
        text_pointer=text_pointer,
        chunks=chunks,
        filename=filename,
    )
    _upsert_user_library(user_id=user_id, book_id=book_id)
    index.add(book_id, sig)

    return IngestResult(
        book_id=book_id,
        was_duplicate=False,
        rows_inserted=rows_inserted,
    )


def ingest(
    *,
    path: Path,
    user_id: UUID,
    client: MilvusClient | None = None,
    dedup_index: Dedup | None = None,
) -> IngestResult:
    """Run the full ingest pipeline. Returns the ``IngestResult``.

    The file at *path* is the raw original; Phase 31 persists it to the
    originals bucket and records the object key in
    ``global_books.text_pointer`` (new books) or backfills a NULL
    pointer (dup-hits) — see ``ingest_markdown``.
    """
    return ingest_markdown(
        markdown=extract(path),
        filename=path.name,
        user_id=user_id,
        client=client,
        dedup_index=dedup_index,
        title=path.stem,
        original=path,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ingest",
        description=(
            "Single-book dedup-aware ingest: detect → extract → MinHash "
            "dedup → (skip on hit | chunk → embed → insert + record). "
            "No Celery yet (Phase 9)."
        ),
    )
    parser.add_argument("path", type=Path, help="Path to an EPUB or PDF.")
    parser.add_argument(
        "--user-id",
        required=True,
        type=UUID,
        help="Owning user UUID; FK to users.user_id (ARCHITECTURE.md §4).",
    )
    args = parser.parse_args(argv)

    result = ingest(path=args.path, user_id=args.user_id)
    if result.was_duplicate:
        sys.stdout.write(
            f"Duplicate detected (book_id={result.book_id}); skipped "
            f"chunking and embedding. Recorded user_library entry for "
            f"user_id={args.user_id}.\n"
        )
    else:
        sys.stdout.write(
            f"New book book_id={result.book_id} — inserted "
            f"{result.rows_inserted} vector row(s) into "
            f"'{COLLECTION_NAME}'; wrote global_books + user_library.\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
