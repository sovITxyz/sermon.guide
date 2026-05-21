"""Backfill the ``chunks`` table from Milvus for pre-Phase-12 books.

Phase 11 ingests wrote Milvus vectors + ``global_books`` + ``user_library``
but no ``chunks`` row — the table didn't exist yet. Phase 12 (ADR 0004)
introduced ``chunks`` for the BM25 arm of hybrid retrieval. This script
walks every ``global_books`` row that lacks chunks and rehydrates the
Postgres side from what Milvus already holds.

Idempotent: per-book inserts use ``ON CONFLICT DO NOTHING`` against the
``uq_chunks_book_chunk`` constraint, and a book that already has any
chunks is skipped entirely (the first-write-wins policy assumes Phase 12
ingest writes atomically — see ``ingest.py:_insert_book_with_chunks``).

Usage (from ``worker/``):

    uv run python -m scripts.backfill_chunks            # all missing
    uv run python -m scripts.backfill_chunks --dry-run  # report only
    uv run python -m scripts.backfill_chunks --book-id <uuid>  # one book
"""

# pymilvus 2.6 ships without `py.typed`; same relaxations as bootstrap_milvus.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnnecessaryComparison=false

from __future__ import annotations

import argparse
import sys
import uuid
from typing import Any

from pymilvus import MilvusClient
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from db import Chunk, GlobalBook, get_sync_session_factory
from scripts.bootstrap_milvus import COLLECTION_NAME, make_client

# Milvus pagination — pull this many vectors per ``query`` call. Keep
# moderate so very large books don't blow memory; the loop continues
# until the iterator drains.
_QUERY_BATCH = 1000


def _books_missing_chunks(session: Session) -> list[uuid.UUID]:
    """Return ``global_books`` ids that have zero rows in ``chunks``."""
    stmt = (
        select(GlobalBook.book_id)
        .outerjoin(Chunk, Chunk.book_id == GlobalBook.book_id)
        .group_by(GlobalBook.book_id)
        .having(func.count(Chunk.chunk_id) == 0)
    )
    return list(session.execute(stmt).scalars().all())


def _fetch_chunks_for_book(
    *,
    client: MilvusClient,
    book_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Pull every Milvus row for *book_id*, paginated via ``query_iterator``.

    Returns dicts shaped like the Phase 6 ingest writes:
    ``{book_id, content_chunk, metadata={filename, chunk_index, parent_section}}``.
    """
    expr = f'book_id == "{book_id!s}"'
    rows: list[dict[str, Any]] = []
    iterator = client.query_iterator(
        collection_name=COLLECTION_NAME,
        filter=expr,
        output_fields=["book_id", "content_chunk", "metadata"],
        batch_size=_QUERY_BATCH,
    )
    try:
        while True:
            batch = iterator.next()
            if not batch:
                break
            rows.extend(batch)
    finally:
        iterator.close()
    return rows


def _insert_chunks(
    *,
    session: Session,
    book_id: uuid.UUID,
    milvus_rows: list[dict[str, Any]],
) -> int:
    """Bulk-insert one book's chunks; ON CONFLICT DO NOTHING for idempotency.

    Returns the number of rows the INSERT *attempted* to write. The
    actual rows-affected count would require ``RETURNING``; for backfill
    progress reporting the attempted count is what the operator wants.
    """
    if not milvus_rows:
        return 0
    payload: list[dict[str, Any]] = []
    for r in milvus_rows:
        metadata = r["metadata"]
        payload.append(
            {
                "chunk_id": uuid.uuid4(),
                "book_id": book_id,
                "chunk_index": int(metadata["chunk_index"]),
                "content": r["content_chunk"],
                "parent_section": metadata.get("parent_section"),
                "filename": metadata.get("filename", ""),
            },
        )
    stmt = (
        pg_insert(Chunk)
        .values(payload)
        .on_conflict_do_nothing(
            constraint="uq_chunks_book_chunk",
        )
    )
    session.execute(stmt)
    return len(payload)


def backfill(*, only_book: uuid.UUID | None = None, dry_run: bool = False) -> int:
    """Backfill missing ``chunks`` rows. Returns the exit code."""
    client = make_client()
    if not client.has_collection(collection_name=COLLECTION_NAME):
        sys.stderr.write(
            f"Milvus collection '{COLLECTION_NAME}' not found — run "
            "`make bootstrap-milvus` first.\n",
        )
        return 2

    sf = get_sync_session_factory()
    # Discover work in one short read-only session; per-book inserts get
    # their own session below so each book commits independently and a
    # large corpus doesn't pile up a single mega-transaction.
    with sf() as session:
        if only_book is not None:
            book_ids = [only_book]
        else:
            book_ids = _books_missing_chunks(session)

    if not book_ids:
        sys.stdout.write("No books need backfilling — chunks are up to date.\n")
        return 0

    sys.stdout.write(
        f"{'[dry-run] ' if dry_run else ''}Backfilling {len(book_ids)} book(s).\n",
    )

    total_chunks = 0
    for book_id in book_ids:
        milvus_rows = _fetch_chunks_for_book(client=client, book_id=book_id)
        sys.stdout.write(f"  {book_id}: {len(milvus_rows)} Milvus row(s)\n")
        if dry_run:
            total_chunks += len(milvus_rows)
            continue
        with sf() as session, session.begin():
            attempted = _insert_chunks(
                session=session,
                book_id=book_id,
                milvus_rows=milvus_rows,
            )
        total_chunks += attempted

    sys.stdout.write(
        f"{'[dry-run] ' if dry_run else ''}"
        f"Done — {total_chunks} chunk row(s) {'would be' if dry_run else ''} written.\n",
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backfill_chunks",
        description=(
            "Backfill the chunks table from Milvus for books that predate "
            "Phase 12. Idempotent; safe to re-run."
        ),
    )
    parser.add_argument(
        "--book-id",
        type=uuid.UUID,
        default=None,
        help="Backfill a single book (defaults to all missing).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be written; don't touch Postgres.",
    )
    args = parser.parse_args(argv)
    return backfill(only_book=args.book_id, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
