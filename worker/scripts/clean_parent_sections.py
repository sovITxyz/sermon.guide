"""Clean HTML tag soup out of stored ``parent_section`` values (Phase 21).

Before Phase 21, ``worker/chunking.py`` captured raw pandoc heading text,
so inline-HTML debris like ``<a href="part0002.html#..."><span`` landed in
both stores: Postgres ``chunks.parent_section`` and the ``parent_section``
key inside Milvus row metadata. Capture is fixed (``_heading_offsets`` now
routes through ``chunking.clean_heading``); this script backfills rows
written before the fix, importing that exact function so capture-time and
backfill-time semantics can never drift (see ``chunking.clean_heading``'s
docstring and worker/AGENTS.md).

Per book (identity between the stores is ``(book_id, chunk_index)``):

- **Postgres** — ``UPDATE chunks SET parent_section = <cleaned>`` keyed by
  ``(book_id, chunk_index)``. A value that strips to the empty string
  becomes ``NULL``; ``''`` is never stored.
- **Milvus** — ``library_vectors`` has an INT64 ``auto_id`` PK and no
  partial JSON update, so each dirty row is rewritten by query (including
  the vector) → delete by id → immediate reinsert. ``vector``,
  ``book_id``, and ``content_chunk`` are reinserted byte-identical (the
  shallow row copy keeps the very objects the query returned; float32
  round-trips exactly); only ``metadata.parent_section`` changes and every
  other metadata key is preserved. Reinsert mints NEW auto-ids — nothing
  references the old ones today (``highlights.vector_id`` is never
  written), but future code must not assume id stability across this pass.

The two stores are detected and cleaned independently, so a run that dies
between them converges on re-run, and the script is idempotent — a second
run finds zero dirty rows. A row dirty in one store with no
``(book_id, chunk_index)`` counterpart in the other is reported to stderr
and still cleaned where it exists — never a crash.

Scope: books present in ``global_books`` (every ``chunks`` row's FK target).
Milvus rows under orphan book_ids are ``scripts.sweep_orphans``'s job —
they get deleted, not cleaned.

DESTRUCTIVE (Milvus delete + reinsert), so unlike ``backfill_chunks`` the
default here is a dry-run; pass ``--execute`` to apply.

Usage (from ``worker/``):

    uv run python -m scripts.clean_parent_sections                  # dry-run, all books
    uv run python -m scripts.clean_parent_sections --execute        # apply
    uv run python -m scripts.clean_parent_sections --book-id <uuid> # one book
"""

# pymilvus 2.6 ships without `py.typed`; same relaxations as bootstrap_milvus.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnnecessaryComparison=false

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pymilvus import MilvusClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from chunking import clean_heading
from db import Chunk, GlobalBook, get_sync_session_factory
from scripts.bootstrap_milvus import COLLECTION_NAME, make_client

# Milvus pagination — same knob as backfill_chunks, but rows here carry the
# 1024-dim vector (~4-8 MB per batch as Python floats); keep it moderate.
_QUERY_BATCH = 1000

# Delete + reinsert sub-batch. Each batch's delete is immediately followed
# by its reinsert so the window where a row exists only in process memory
# stays one batch wide (PG holds content but NOT embeddings — losing a row
# here would mean re-embedding).
_REWRITE_BATCH = 500

# The query must return every insert-shape field plus `id` (for the
# targeted delete) and `vector` (so the reinsert is byte-identical).
_OUTPUT_FIELDS = ["id", "vector", "book_id", "content_chunk", "metadata"]


def target_parent_section(raw: str | None) -> str | None:
    """The value a stored ``parent_section`` should hold after Phase 21.

    Reuses ``chunking.clean_heading`` — the capture-time sanitizer — as the
    single source of truth. A heading that strips to the empty string
    (anchor-only / truncated tag fragments) maps to ``None``: the capture
    path never stores ``''`` and neither does the backfill.
    """
    if raw is None:
        return None
    return clean_heading(raw) or None


def is_dirty(raw: object) -> bool:
    """True when a stored ``parent_section`` value needs rewriting.

    Accepts ``object`` because Milvus metadata is untyped JSON; anything
    that isn't a string (``None`` included) is left alone.
    """
    return isinstance(raw, str) and target_parent_section(raw) != raw


def corrected_milvus_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Build the reinsert payload for a dirty Milvus row, or None if clean.

    Drops the ``id`` key (the auto_id PK must not be supplied on insert),
    keeps every other top-level field by reference (vector and content
    round-trip identical), and rewrites only ``metadata.parent_section`` —
    all other metadata keys are preserved verbatim.
    """
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("parent_section")
    if not isinstance(raw, str):
        return None
    cleaned = target_parent_section(raw)
    if cleaned == raw:
        return None
    new_metadata: dict[str, Any] = dict(metadata)
    new_metadata["parent_section"] = cleaned
    fixed = {k: v for k, v in row.items() if k != "id"}
    fixed["metadata"] = new_metadata
    return fixed


@dataclass(frozen=True, slots=True)
class BookReport:
    """Per-book dirty counts + cross-store mismatches (reported, not fatal)."""

    book_id: uuid.UUID
    pg_dirty: int
    milvus_dirty: int
    # chunk_index values dirty in PG with no Milvus row at all.
    pg_missing_in_milvus: tuple[int, ...]
    # chunk_index values dirty in Milvus with no PG chunks row at all.
    milvus_missing_in_pg: tuple[int, ...]


def _fetch_book_rows(*, client: MilvusClient, book_id: uuid.UUID) -> list[dict[str, Any]]:
    """Drain every Milvus row for *book_id* (vector included), paginated.

    ``book_id`` is a UUID object minted from Postgres, never client input,
    so the filter interpolation cannot be injected into (same reasoning as
    ``ingest._scrub_partial_vectors``).
    """
    expr = f'book_id == "{book_id!s}"'
    rows: list[dict[str, Any]] = []
    iterator = client.query_iterator(
        collection_name=COLLECTION_NAME,
        filter=expr,
        output_fields=_OUTPUT_FIELDS,
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


def _update_pg_rows(
    *,
    session: Session,
    book_id: uuid.UUID,
    updates: dict[int, str | None],
) -> None:
    """Apply cleaned values keyed by ``(book_id, chunk_index)`` — parameterized."""
    for chunk_index, value in sorted(updates.items()):
        session.execute(
            update(Chunk)
            .where(Chunk.book_id == book_id, Chunk.chunk_index == chunk_index)
            .values(parent_section=value),
        )


def _rewrite_milvus_rows(
    *,
    client: MilvusClient,
    dirty_ids: list[int],
    corrected: list[dict[str, Any]],
) -> None:
    """Delete dirty rows by id and reinsert the corrected payloads.

    Query happened BEFORE any delete (rows are already in memory); each
    delete batch is immediately followed by its reinsert, then one flush +
    load mirrors the ingest write path (``ingest.py`` insert → flush →
    load_collection).
    """
    for start in range(0, len(corrected), _REWRITE_BATCH):
        batch_ids = dirty_ids[start : start + _REWRITE_BATCH]
        batch_rows = corrected[start : start + _REWRITE_BATCH]
        client.delete(collection_name=COLLECTION_NAME, ids=batch_ids)
        client.insert(collection_name=COLLECTION_NAME, data=batch_rows)
    client.flush(collection_name=COLLECTION_NAME)
    client.load_collection(collection_name=COLLECTION_NAME)


def _clean_book(
    *,
    client: MilvusClient,
    session_factory: sessionmaker[Session],
    book_id: uuid.UUID,
    execute: bool,
) -> BookReport:
    """Detect (and with *execute*, fix) one book's dirty rows in both stores."""
    # --- Postgres side: detect ---------------------------------------------
    with session_factory() as session:
        pg_rows = session.execute(
            select(Chunk.chunk_index, Chunk.parent_section).where(Chunk.book_id == book_id),
        ).all()
    pg_indexes = {chunk_index for chunk_index, _ in pg_rows}
    pg_updates: dict[int, str | None] = {
        chunk_index: target_parent_section(parent_section)
        for chunk_index, parent_section in pg_rows
        if is_dirty(parent_section)
    }

    # --- Milvus side: detect (query BEFORE any delete) ----------------------
    milvus_rows = _fetch_book_rows(client=client, book_id=book_id)
    milvus_indexes: set[int] = set()
    dirty_ids: list[int] = []
    dirty_indexes: list[int] = []
    corrected: list[dict[str, Any]] = []
    for row in milvus_rows:
        metadata = row.get("metadata")
        chunk_index: int | None = None
        if isinstance(metadata, dict) and metadata.get("chunk_index") is not None:
            chunk_index = int(metadata["chunk_index"])
            milvus_indexes.add(chunk_index)
        fixed = corrected_milvus_row(row)
        if fixed is not None:
            corrected.append(fixed)
            dirty_ids.append(int(row["id"]))
            if chunk_index is not None:
                dirty_indexes.append(chunk_index)

    report = BookReport(
        book_id=book_id,
        pg_dirty=len(pg_updates),
        milvus_dirty=len(corrected),
        pg_missing_in_milvus=tuple(sorted(i for i in pg_updates if i not in milvus_indexes)),
        milvus_missing_in_pg=tuple(sorted(i for i in dirty_indexes if i not in pg_indexes)),
    )

    if not execute:
        return report

    # --- Apply: PG first (its own transaction), then Milvus. The stores are
    # detected independently, so a crash between them converges on re-run.
    if pg_updates:
        with session_factory() as session, session.begin():
            _update_pg_rows(session=session, book_id=book_id, updates=pg_updates)
    if corrected:
        _rewrite_milvus_rows(client=client, dirty_ids=dirty_ids, corrected=corrected)
    return report


def clean(*, only_book: uuid.UUID | None = None, execute: bool = False) -> int:
    """Clean ``parent_section`` debris in both stores. Returns the exit code."""
    client = make_client()
    if not client.has_collection(collection_name=COLLECTION_NAME):
        sys.stderr.write(
            f"Milvus collection '{COLLECTION_NAME}' not found — run "
            "`make bootstrap-milvus` first.\n",
        )
        return 2

    session_factory = get_sync_session_factory()
    with session_factory() as session:
        if only_book is not None:
            book_ids = [only_book]
        else:
            book_ids = list(session.execute(select(GlobalBook.book_id)).scalars().all())

    if not book_ids:
        sys.stdout.write("No books found — nothing to clean.\n")
        return 0

    mode = "" if execute else "[dry-run] "
    sys.stdout.write(f"{mode}Scanning {len(book_ids)} book(s) for parent_section debris.\n")

    total_pg = 0
    total_milvus = 0
    for book_id in book_ids:
        report = _clean_book(
            client=client,
            session_factory=session_factory,
            book_id=book_id,
            execute=execute,
        )
        total_pg += report.pg_dirty
        total_milvus += report.milvus_dirty
        sys.stdout.write(
            f"  {book_id}: pg_dirty={report.pg_dirty} milvus_dirty={report.milvus_dirty}\n",
        )
        if report.pg_missing_in_milvus:
            sys.stderr.write(
                f"  WARNING {book_id}: {len(report.pg_missing_in_milvus)} PG-dirty "
                f"chunk_index value(s) have no Milvus row (PG was still updated): "
                f"{list(report.pg_missing_in_milvus)}\n",
            )
        if report.milvus_missing_in_pg:
            sys.stderr.write(
                f"  WARNING {book_id}: {len(report.milvus_missing_in_pg)} Milvus-dirty "
                f"chunk_index value(s) have no chunks row (Milvus was still cleaned): "
                f"{list(report.milvus_missing_in_pg)}\n",
            )

    update_verb = "updated" if execute else "would update"
    rewrite_verb = "rewrote" if execute else "would rewrite"
    sys.stdout.write(
        f"{mode}Done — {update_verb} {total_pg} Postgres row(s); {rewrite_verb} "
        f"{total_milvus} Milvus row(s) across {len(book_ids)} book(s).\n",
    )
    if not execute and (total_pg or total_milvus):
        sys.stdout.write("Re-run with --execute to apply.\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="clean_parent_sections",
        description=(
            "Strip HTML debris from chunks.parent_section (Postgres) and the "
            "matching Milvus metadata. Dry-run by default — this rewrites "
            "Milvus rows via delete+reinsert; pass --execute to apply."
        ),
    )
    parser.add_argument(
        "--book-id",
        type=uuid.UUID,
        default=None,
        help="Clean a single book (defaults to every global_books row).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the changes (default is a report-only dry run).",
    )
    args = parser.parse_args(argv)
    return clean(only_book=args.book_id, execute=args.execute)


if __name__ == "__main__":
    raise SystemExit(main())
