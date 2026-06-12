"""Sweep orphaned book debris out of Milvus and Postgres (Phase 21).

Two orphan classes, detected from a full inventory of both stores:

(a) **Milvus-only** — ``library_vectors`` rows whose ``book_id`` has no
    ``global_books`` row (dev/test ingests that never landed, e.g. the
    Phase 6 ``b_phase6_real_epub`` debris).
(b) **Postgres-only** — ``global_books`` rows with zero ``user_library``
    refs AND zero ``chunks`` AND zero Milvus vectors.

Safety rules (tenant isolation is not negotiable):

- **HARD REFUSAL** — any candidate with a ``user_library`` reference
  aborts the entire run (exit 3) before anything is deleted:
  tenant-reachable data is never swept. References are re-checked fresh
  immediately before deleting, and for class (b) the database
  double-enforces the rule via ``user_library``'s RESTRICT FK.
- Books holding an in-flight ingest claim (``upload_tasks.book_id``,
  Phase 20) are skipped: during the claim window a live ingest
  legitimately has Milvus vectors with no ``global_books`` row yet. A dev
  DB that predates the ``upload_tasks`` migration is detected via
  ``to_regclass`` and treated as having no claims.
- Books with any chunks or any library refs are never candidates — the
  sweep touches orphans only. ``chunks``/``highlights`` cascade with
  their ``global_books`` row, so class (b) needs no manual child deletes
  (and has zero chunks by definition anyway).
- Milvus delete filters are built only from book_ids matching a strict
  allowlist pattern (``book_id_expr``); anything else is reported and
  left for manual cleanup — never interpolated into an expr.

Idempotent: a second run finds zero candidates. Exact before/after row
counts for both stores are printed (the after-counts come from a fresh
post-flush rescan, not arithmetic).

DESTRUCTIVE, so unlike ``backfill_chunks`` the default is a dry-run that
lists candidates with their counts; pass ``--execute`` to delete.

Usage (from ``worker/``):

    uv run python -m scripts.sweep_orphans            # dry-run, list candidates
    uv run python -m scripts.sweep_orphans --execute  # delete orphans
"""

# pymilvus 2.6 ships without `py.typed`; same relaxations as bootstrap_milvus.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnnecessaryComparison=false

from __future__ import annotations

import argparse
import re
import sys
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

from pymilvus import MilvusClient
from sqlalchemy import CursorResult, delete, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from db import Chunk, GlobalBook, UploadTask, UserLibraryEntry, get_sync_session_factory
from scripts.bootstrap_milvus import COLLECTION_NAME, make_client

_QUERY_BATCH = 1000

# Milvus expr-literal allowlist. Every legitimate book_id is either a
# canonical UUID string or a legacy dev label like `b_phase6_real_epub`;
# both fit [A-Za-z0-9._-]. Anything else (quotes, backslashes, spaces,
# control chars) is refused rather than escaped — there is no trusted
# escaping story for Milvus expr strings, so we never build one from an
# unvetted value. VARCHAR max_length=64 bounds the length.
_SAFE_BOOK_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Exit codes.
_EXIT_OK = 0
_EXIT_NO_COLLECTION = 2
_EXIT_TENANT_REFUSAL = 3


def book_id_expr(book_id: str) -> str:
    """Build the Milvus delete/count filter for one allowlisted book_id.

    Raises ``ValueError`` for any id outside the strict allowlist — such
    ids are never interpolated into an expr (injection hygiene; see the
    module docstring).
    """
    if _SAFE_BOOK_ID.fullmatch(book_id) is None:
        msg = (
            f"book_id {book_id!r} fails the expr-safety allowlist "
            f"{_SAFE_BOOK_ID.pattern!r}; refusing to build a Milvus filter for it"
        )
        raise ValueError(msg)
    return f'book_id == "{book_id}"'


@dataclass(frozen=True, slots=True)
class BookFacts:
    """Everything the classifier needs to know about one book_id.

    ``book_id`` is the canonical string form — ``str(uuid)`` for Postgres
    rows, the raw VARCHAR for Milvus-only ids (which may be non-UUID dev
    labels that structurally cannot exist in Postgres).
    """

    book_id: str
    in_global_books: bool
    chunk_count: int
    library_ref_count: int
    vector_count: int
    claimed: bool


@dataclass(frozen=True, slots=True)
class SweepPlan:
    """Classification output: what to delete, what to refuse, what to skip."""

    milvus_orphans: tuple[BookFacts, ...]
    pg_orphans: tuple[BookFacts, ...]
    # Candidate-shaped books that a tenant can reach — their existence
    # aborts the whole run.
    refusals: tuple[BookFacts, ...]
    # Candidate-shaped books with an in-flight ingest claim — never touched.
    skipped_claims: tuple[BookFacts, ...]


def classify(facts: Iterable[BookFacts]) -> SweepPlan:
    """Pure orphan classification — no I/O, unit-tested in isolation.

    A book is *candidate-shaped* when it is either (a) Milvus-only
    (vectors, no ``global_books`` row) or (b) an empty ``global_books``
    row (zero chunks, zero vectors). Candidates with library refs become
    refusals; claimed candidates are skipped; everything else — any book
    with chunks, with refs, or mid-ingest vectors alongside its
    ``global_books`` row — is left alone.
    """
    milvus_orphans: list[BookFacts] = []
    pg_orphans: list[BookFacts] = []
    refusals: list[BookFacts] = []
    skipped_claims: list[BookFacts] = []
    for fact in facts:
        candidate_milvus_only = not fact.in_global_books and fact.vector_count > 0
        candidate_pg_empty = (
            fact.in_global_books and fact.chunk_count == 0 and fact.vector_count == 0
        )
        if not (candidate_milvus_only or candidate_pg_empty):
            continue
        if fact.library_ref_count > 0:
            refusals.append(fact)
        elif fact.claimed:
            skipped_claims.append(fact)
        elif candidate_milvus_only:
            milvus_orphans.append(fact)
        else:
            pg_orphans.append(fact)
    return SweepPlan(
        milvus_orphans=tuple(milvus_orphans),
        pg_orphans=tuple(pg_orphans),
        refusals=tuple(refusals),
        skipped_claims=tuple(skipped_claims),
    )


def _as_uuid(book_id: str) -> uuid.UUID | None:
    """Parse a book_id string as UUID, or None for legacy non-UUID dev ids."""
    try:
        return uuid.UUID(book_id)
    except ValueError:
        return None


def _scan_milvus_counts(client: MilvusClient) -> dict[str, int]:
    """Full-collection scan → ``{book_id: vector_count}``.

    A real row scan (not ``get_collection_stats``) so deletes are
    reflected exactly; at dev scale (~1.5k rows) this is trivial.
    """
    counts: dict[str, int] = {}
    iterator = client.query_iterator(
        collection_name=COLLECTION_NAME,
        filter="",
        output_fields=["book_id"],
        batch_size=_QUERY_BATCH,
    )
    try:
        while True:
            batch = iterator.next()
            if not batch:
                break
            for row in batch:
                book_id = str(row["book_id"])
                counts[book_id] = counts.get(book_id, 0) + 1
    finally:
        iterator.close()
    return counts


def _claimed_book_ids(session: Session) -> set[str]:
    """In-flight ingest claims (Phase 20) — book_ids the sweep must skip.

    ``upload_tasks.book_id`` deliberately has no FK: it names a book whose
    ``global_books`` row may not have landed yet. A dev DB from before the
    Phase 20 migration has no table at all — detected, not crashed on.
    """
    table_exists = bool(
        session.execute(
            text("SELECT to_regclass('public.upload_tasks') IS NOT NULL"),
        ).scalar_one(),
    )
    if not table_exists:
        sys.stderr.write(
            "note: upload_tasks table absent (pre-Phase-20 dev DB) — "
            "treating as zero in-flight claims.\n",
        )
        return set()
    rows = session.execute(
        select(UploadTask.book_id).where(UploadTask.book_id.is_not(None)),
    ).scalars()
    return {str(book_id) for book_id in rows}


def _library_ref_count(session: Session, book_id: str) -> int:
    """Fresh ``user_library`` ref count; non-UUID ids structurally have none."""
    book_uuid = _as_uuid(book_id)
    if book_uuid is None:
        return 0
    return session.execute(
        select(func.count())
        .select_from(UserLibraryEntry)
        .where(UserLibraryEntry.book_id == book_uuid),
    ).scalar_one()


def _gather_facts(*, client: MilvusClient, session: Session) -> list[BookFacts]:
    """Inventory both stores into one ``BookFacts`` row per known book_id."""
    milvus_counts = _scan_milvus_counts(client)
    global_ids = {str(b) for b in session.execute(select(GlobalBook.book_id)).scalars()}
    chunk_counts = {
        str(book_id): int(n)
        for book_id, n in session.execute(
            select(Chunk.book_id, func.count()).group_by(Chunk.book_id),
        ).all()
    }
    ref_counts = {
        str(book_id): int(n)
        for book_id, n in session.execute(
            select(UserLibraryEntry.book_id, func.count()).group_by(UserLibraryEntry.book_id),
        ).all()
    }
    claimed = _claimed_book_ids(session)
    return [
        BookFacts(
            book_id=book_id,
            in_global_books=book_id in global_ids,
            chunk_count=chunk_counts.get(book_id, 0),
            library_ref_count=ref_counts.get(book_id, 0),
            vector_count=milvus_counts.get(book_id, 0),
            claimed=book_id in claimed,
        )
        for book_id in sorted(set(milvus_counts) | global_ids)
    ]


def _pg_totals(session: Session) -> tuple[int, int, int]:
    """(global_books, chunks, user_library) row counts."""
    global_books = session.execute(select(func.count()).select_from(GlobalBook)).scalar_one()
    chunks = session.execute(select(func.count()).select_from(Chunk)).scalar_one()
    library = session.execute(select(func.count()).select_from(UserLibraryEntry)).scalar_one()
    return global_books, chunks, library


def _print_plan(plan: SweepPlan) -> None:
    sys.stdout.write("Milvus-only orphans (vectors with no global_books row):\n")
    if plan.milvus_orphans:
        for fact in plan.milvus_orphans:
            sys.stdout.write(
                f"  {fact.book_id}: vectors={fact.vector_count} "
                f"chunks={fact.chunk_count} library_refs={fact.library_ref_count}\n",
            )
    else:
        sys.stdout.write("  (none)\n")
    sys.stdout.write("global_books orphan rows (zero refs, zero chunks, zero vectors):\n")
    if plan.pg_orphans:
        for fact in plan.pg_orphans:
            sys.stdout.write(
                f"  {fact.book_id}: vectors={fact.vector_count} "
                f"chunks={fact.chunk_count} library_refs={fact.library_ref_count}\n",
            )
    else:
        sys.stdout.write("  (none)\n")
    for fact in plan.skipped_claims:
        sys.stdout.write(
            f"Skipping {fact.book_id}: in-flight upload_tasks claim — a live "
            f"ingest may be mid-write; re-run once workers are idle.\n",
        )


def _print_refusals(refusals: tuple[BookFacts, ...]) -> None:
    sys.stderr.write(
        "ABORT: candidate(s) below are reachable through user_library — "
        "tenant-reachable data is never swept. Nothing was deleted.\n",
    )
    for fact in refusals:
        sys.stderr.write(
            f"  {fact.book_id}: library_refs={fact.library_ref_count} "
            f"chunks={fact.chunk_count} vectors={fact.vector_count}\n",
        )


def _execute_sweep(
    *,
    client: MilvusClient,
    session_factory: sessionmaker[Session],
    plan: SweepPlan,
) -> int:
    """Apply the plan. Returns the exit code.

    Pre-flight: every candidate's ``user_library`` refs are re-checked
    fresh — if any appeared since the scan, abort before touching either
    store. Then class (a) Milvus deletes, then class (b) in FK-safe order
    (refs confirmed zero → Milvus vectors → ``global_books`` row, which
    cascades chunks/highlights; RESTRICT on ``user_library`` is the DB's
    own backstop).
    """
    candidates = plan.milvus_orphans + plan.pg_orphans
    with session_factory() as session:
        recheck_refusals = tuple(
            fact for fact in candidates if _library_ref_count(session, fact.book_id) > 0
        )
    if recheck_refusals:
        _print_refusals(recheck_refusals)
        return _EXIT_TENANT_REFUSAL

    unsafe_ids: list[str] = []
    deleted_vectors = 0
    deleted_books = 0

    # (a) Milvus-only orphans — delete by validated book_id filter.
    for fact in plan.milvus_orphans:
        try:
            expr = book_id_expr(fact.book_id)
        except ValueError as exc:
            sys.stderr.write(
                f"WARNING: {exc} — its {fact.vector_count} vector(s) were left in "
                "place; delete manually via the pymilvus client.\n"
            )
            unsafe_ids.append(fact.book_id)
            continue
        with session_factory() as session:
            book_uuid = _as_uuid(fact.book_id)
            landed = book_uuid is not None and (
                session.execute(
                    select(GlobalBook.book_id).where(GlobalBook.book_id == book_uuid),
                ).first()
                is not None
            )
        if landed:
            sys.stderr.write(
                f"Skipping {fact.book_id}: a global_books row appeared since the "
                f"scan — no longer an orphan.\n",
            )
            continue
        client.delete(collection_name=COLLECTION_NAME, filter=expr)
        deleted_vectors += fact.vector_count
        sys.stdout.write(f"  deleted Milvus vectors: {fact.book_id} ({fact.vector_count})\n")

    # (b) Empty global_books rows — vectors first (zero by definition, the
    # delete is a no-op guard), then the row itself.
    for fact in plan.pg_orphans:
        with session_factory() as session, session.begin():
            book_uuid = _as_uuid(fact.book_id)
            if book_uuid is None:  # unreachable: PG ids are UUIDs by schema
                continue
            refs = _library_ref_count(session, fact.book_id)
            if refs > 0:
                _print_refusals((fact,))
                return _EXIT_TENANT_REFUSAL
            chunk_count = session.execute(
                select(func.count()).select_from(Chunk).where(Chunk.book_id == book_uuid),
            ).scalar_one()
            if chunk_count > 0:
                sys.stderr.write(
                    f"Skipping {fact.book_id}: chunks appeared since the scan — "
                    f"no longer an orphan.\n",
                )
                continue
            client.delete(collection_name=COLLECTION_NAME, filter=book_id_expr(fact.book_id))
            # Session.execute is typed Result[Any]; DML statements return a
            # CursorResult at runtime, which carries rowcount.
            result = cast(
                "CursorResult[Any]",
                session.execute(delete(GlobalBook).where(GlobalBook.book_id == book_uuid)),
            )
            deleted_books += result.rowcount
            sys.stdout.write(f"  deleted global_books row: {fact.book_id}\n")

    client.flush(collection_name=COLLECTION_NAME)
    client.load_collection(collection_name=COLLECTION_NAME)

    sys.stdout.write(
        f"Swept {deleted_vectors} Milvus vector(s) and {deleted_books} global_books row(s).\n",
    )
    if unsafe_ids:
        sys.stderr.write(
            f"WARNING: {len(unsafe_ids)} book_id(s) failed the expr-safety "
            f"allowlist and were NOT swept: {unsafe_ids}\n",
        )
    return _EXIT_OK


def sweep(*, execute: bool = False) -> int:
    """Detect (and with *execute*, delete) orphan debris. Returns exit code."""
    client = make_client()
    if not client.has_collection(collection_name=COLLECTION_NAME):
        sys.stderr.write(
            f"Milvus collection '{COLLECTION_NAME}' not found — run "
            "`make bootstrap-milvus` first.\n",
        )
        return _EXIT_NO_COLLECTION

    session_factory = get_sync_session_factory()
    with session_factory() as session:
        facts = _gather_facts(client=client, session=session)
        pg_before = _pg_totals(session)
    milvus_before = sum(fact.vector_count for fact in facts if fact.vector_count > 0)

    plan = classify(facts)

    mode = "" if execute else "[dry-run] "
    sys.stdout.write(
        f"{mode}Before: milvus_rows={milvus_before} global_books={pg_before[0]} "
        f"chunks={pg_before[1]} user_library={pg_before[2]}\n",
    )
    _print_plan(plan)

    if plan.refusals:
        _print_refusals(plan.refusals)
        return _EXIT_TENANT_REFUSAL

    if not plan.milvus_orphans and not plan.pg_orphans:
        sys.stdout.write("No orphans to sweep.\n")
        return _EXIT_OK

    if not execute:
        would_vectors = sum(f.vector_count for f in plan.milvus_orphans + plan.pg_orphans)
        sys.stdout.write(
            f"[dry-run] Would delete {would_vectors} Milvus vector(s) across "
            f"{len(plan.milvus_orphans)} book_id(s) and {len(plan.pg_orphans)} "
            f"global_books row(s). Re-run with --execute to apply.\n",
        )
        return _EXIT_OK

    code = _execute_sweep(client=client, session_factory=session_factory, plan=plan)
    if code != _EXIT_OK:
        return code

    # Fresh post-flush rescan — exact after-counts, not arithmetic.
    milvus_after = sum(_scan_milvus_counts(client).values())
    with session_factory() as session:
        pg_after = _pg_totals(session)
    sys.stdout.write(
        f"After:  milvus_rows={milvus_after} global_books={pg_after[0]} "
        f"chunks={pg_after[1]} user_library={pg_after[2]}\n",
    )
    return _EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sweep_orphans",
        description=(
            "Delete orphaned Milvus vectors (no global_books row) and empty "
            "global_books rows (no refs, no chunks, no vectors). Dry-run by "
            "default; pass --execute to delete. Aborts if any candidate is "
            "tenant-reachable."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the deletions (default is a report-only dry run).",
    )
    args = parser.parse_args(argv)
    return sweep(execute=args.execute)


if __name__ == "__main__":
    raise SystemExit(main())
