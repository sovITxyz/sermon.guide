"""Idempotent bulk-ingest of the public-domain seed corpus (Phase 23).

Reads the tracked seed manifest (``worker/seeds/manifest.jsonl`` — the
repo's auditable rights record, one JSON object per line; policy in
``docs/CORPUS_POLICY.md``), looks for each entry's expected file in a
directory of legally held ebooks (default ``worker/tests/samples/``,
which is gitignored — the repo NEVER holds the ebook bytes, only the
manifest), and enqueues every present file through the EXISTING
Celery + dedup ingest path (``tasks.ingest.ingest_book``).

## Idempotency (two layers, both deterministic)

- **Content dedup (Phase 8)** — always on. Re-running the seeder over an
  already-ingested corpus short-circuits at the MinHash gate per book:
  every result converges to ``{was_duplicate: true, rows_inserted: 0}``,
  zero new vectors, zero new ``global_books`` rows.
- **Task-id claim (Phase 20)** — unlike ``make enqueue`` (claim-less,
  legacy Phase 9 posture), the seeder mints the Celery task id
  DETERMINISTICALLY — ``derive_task_id`` is
  ``uuid5(SEED_TASK_NAMESPACE, "<sha256(file)>:<user_id>")`` — and
  commits the matching ``upload_tasks`` row BEFORE ``apply_async`` (the
  ``api/uploads.py`` ordering). A re-run after a crash inside the
  Milvus-flush → Postgres-commit window re-enqueues the SAME task id,
  finds the recorded ``book_id`` claim, scrubs the partial vectors, and
  re-runs under the same ``book_id`` (``ingest.py`` "Task-id claim") —
  no orphan vectors, no duplicate vector sets. Deriving the token from
  the file's sha256 (never the filename) means a REPLACED file gets a
  fresh token and can never converge onto a stale claim recorded for
  different content. The ``upload_tasks`` upsert is
  ``ON CONFLICT DO NOTHING`` so a surviving claim is preserved — that
  surviving claim is exactly what makes the crashed re-run converge.

## Ownership

Books are seeded under the dedicated **corpus-seed user** — the same
deterministic identity ``make enqueue TENANT=corpus-seed`` resolves:
``uuid5`` of the enqueue label namespace over
``corpus-seed.tenants.sermon.guide.local`` (user_id
``d296b559-28f8-54d6-9577-a5539913335c``, email
``corpus-seed@tenants.sermon.guide.local``), created idempotently via
``scripts.enqueue_ingest.resolve_tenant``. Seeded books land ONLY in
that user's ``user_library`` — never in any tenant's. Other users (and
the golden suite's fixture user) gain access the tenant-correct way:
their own ingest of identical content dedup-hits and upserts their OWN
``user_library`` row onto the shared ``book_id`` (one MinHash + one
upsert — cheap by design).

## Concurrency posture

Default is fully serial (``--max-in-flight 1``): enqueue one book, wait
for its result, then enqueue the next. Ingest runs tens of minutes per
book on CPU while the broker visibility timeout is 300 s — a free worker
slot can pick up a redelivered copy of a STILL-RUNNING task and
interleave with it (the documented Phase 20 residual: the claim is
task-id-keyed, not leased), which can double a book's vectors. Raise
``--max-in-flight`` only to exactly the worker's ``--concurrency`` so
every slot stays busy, and verify per-book vector counts afterwards —
see ``docs/SEED_CORPUS.md`` ("Parallelism"). Never run two seeders
concurrently against the same stack.

Exit codes (mirrors ``scripts/test_live.sh``): 0 = all enqueued books
converged (new or duplicate); 1 = at least one ingest failed/timed out,
or nothing was enqueueable; 2 = environment not wired (no worker
responding / broker unreachable).

Usage (from ``worker/`` — the make target sources ``../infra/.env``):

    make seed-corpus ARGS=--dry-run    # plan only: no DB/broker access
    make seed-corpus                   # enqueue + wait, serial
"""

# Celery 5 ships without `py.typed`; same relaxation pattern as
# tasks/ingest.py / enqueue_ingest.py.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportFunctionMemberAccess=false

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from celery.exceptions import TimeoutError as CeleryTimeoutError
from kombu.exceptions import OperationalError
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db import UploadTask, get_sync_session_factory
from scripts.enqueue_ingest import LABEL_EMAIL_DOMAIN, LABEL_NAMESPACE, resolve_tenant
from tasks.ingest import ingest_book

#: The enqueue-label identity every seeded book is owned by. The same
#: user `make enqueue TENANT=corpus-seed` resolves — see the module
#: docstring ("Ownership").
SEED_TENANT_LABEL = "corpus-seed"

#: Fixed uuid5 namespace for deterministic seed task ids — a sibling of
#: ``scripts.enqueue_ingest.LABEL_NAMESPACE`` (…0001). Changing it would
#: orphan in-flight ``upload_tasks`` claims from earlier seed runs, so it
#: is pinned by a unit test.
SEED_TASK_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-000000000002")

#: Rights policy enforced in code: the seeder refuses any manifest entry
#: that is not explicitly public-domain (docs/CORPUS_POLICY.md).
ALLOWED_LICENSES = frozenset({"public-domain"})
ALLOWED_SOURCES = frozenset({"gutenberg", "ccel"})

_WORKER_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = _WORKER_DIR / "seeds" / "manifest.jsonl"
#: Default file location is the gitignored golden-sample dir on purpose:
#: one download serves both the live seed and the Phase 23 golden rows
#: (the golden fixture resolves books by exact filename under it).
DEFAULT_FILES_DIR = _WORKER_DIR / "tests" / "samples"

_MANIFEST_FIELDS = (
    "title",
    "author",
    "source",
    "source_id",
    "source_url",
    "download_url",
    "license",
    "filename",
)

# Lowercase-kebab basename ending in a worker-supported format. Anchored
# allowlist: no path separators, no leading dots — a manifest filename is
# joined onto --files-dir, so this is also the traversal guard.
_FILENAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\.(epub|pdf)$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_EXIT_OK = 0
_EXIT_FAILED = 1
_EXIT_ENV = 2


@dataclass(frozen=True, slots=True)
class SeedBook:
    """One manifest entry — the auditable rights record for one book."""

    title: str
    author: str
    source: str
    source_id: str
    source_url: str
    download_url: str
    license: str
    filename: str


@dataclass(frozen=True, slots=True)
class SeedPlanItem:
    """Per-book plan: where the file should be and what to enqueue.

    ``sha256_hex`` is ``None`` when the file is absent. ``task_id`` is
    set only for enqueueable items — present files whose content is not
    a byte-identical duplicate of an earlier manifest entry
    (``duplicate_of`` names that earlier filename; enqueueing both would
    reuse one Celery task id for two messages).
    """

    book: SeedBook
    path: Path
    sha256_hex: str | None
    task_id: UUID | None
    duplicate_of: str | None

    @property
    def present(self) -> bool:
        return self.sha256_hex is not None


def parse_manifest_entry(parsed: object, *, lineno: int) -> SeedBook:
    """Validate one decoded manifest line into a ``SeedBook``.

    Strict by design — the manifest is the rights record, so unknown
    fields, missing/empty fields, non-allowlisted licenses or sources,
    and unsafe filenames are all hard errors, never warnings.
    """
    if not isinstance(parsed, dict):
        msg = f"manifest line {lineno}: expected a JSON object, got {type(parsed).__name__}"
        raise TypeError(msg)
    entry = cast("dict[str, object]", parsed)
    unknown = sorted(set(entry) - set(_MANIFEST_FIELDS))
    if unknown:
        msg = f"manifest line {lineno}: unknown field(s) {unknown}"
        raise ValueError(msg)
    values: dict[str, str] = {}
    for field in _MANIFEST_FIELDS:
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            msg = f"manifest line {lineno}: field {field!r} must be a non-empty string"
            raise ValueError(msg)
        values[field] = value
    if values["license"] not in ALLOWED_LICENSES:
        msg = (
            f"manifest line {lineno}: license {values['license']!r} is not in "
            f"{sorted(ALLOWED_LICENSES)} — the seed corpus is public-domain only "
            f"(docs/CORPUS_POLICY.md); the seeder refuses anything else"
        )
        raise ValueError(msg)
    if values["source"] not in ALLOWED_SOURCES:
        msg = (
            f"manifest line {lineno}: source {values['source']!r} is not in "
            f"{sorted(ALLOWED_SOURCES)}"
        )
        raise ValueError(msg)
    if _FILENAME_RE.fullmatch(values["filename"]) is None:
        msg = (
            f"manifest line {lineno}: filename {values['filename']!r} fails the "
            f"allowlist {_FILENAME_RE.pattern!r} (lowercase-kebab basename ending "
            f".epub/.pdf; no path separators)"
        )
        raise ValueError(msg)
    return SeedBook(
        title=values["title"],
        author=values["author"],
        source=values["source"],
        source_id=values["source_id"],
        source_url=values["source_url"],
        download_url=values["download_url"],
        license=values["license"],
        filename=values["filename"],
    )


def load_manifest(path: Path) -> tuple[SeedBook, ...]:
    """Parse and validate the JSONL manifest. Blank lines are tolerated."""
    if not path.is_file():
        msg = f"seed manifest not found: {path}"
        raise FileNotFoundError(msg)
    books: list[SeedBook] = []
    seen_filenames: set[str] = set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed: object = json.loads(line)
        except json.JSONDecodeError as exc:
            msg = f"manifest line {lineno}: invalid JSON: {exc}"
            raise ValueError(msg) from exc
        book = parse_manifest_entry(parsed, lineno=lineno)
        if book.filename in seen_filenames:
            msg = f"manifest line {lineno}: duplicate filename {book.filename!r}"
            raise ValueError(msg)
        seen_filenames.add(book.filename)
        books.append(book)
    if not books:
        msg = f"seed manifest is empty: {path}"
        raise ValueError(msg)
    return tuple(books)


def file_sha256(path: Path) -> str:
    """Streaming sha256 of *path* (lowercase hex)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def derive_task_id(*, sha256_hex: str, user_id: UUID) -> UUID:
    """Deterministic Phase 20 idempotency token for one (content, owner).

    ``uuid5(SEED_TASK_NAMESPACE, "<sha256>:<user_id>")`` — stable across
    re-runs of identical bytes (so a crashed run's ``upload_tasks`` claim
    is found again), different for replaced content (so a stale claim is
    never inherited by a different book).
    """
    if _SHA256_RE.fullmatch(sha256_hex) is None:
        msg = f"sha256_hex {sha256_hex!r} is not 64 lowercase hex chars"
        raise ValueError(msg)
    return uuid.uuid5(SEED_TASK_NAMESPACE, f"{sha256_hex}:{user_id}")


def seed_user_id() -> UUID:
    """Pure derivation of the corpus-seed user id (no DB access).

    MUST stay equal to ``enqueue_ingest.resolve_tenant(SEED_TENANT_LABEL)``'s
    derivation — ``ensure_seed_user`` asserts the parity at runtime and a
    unit test pins the literal UUID.
    """
    return uuid.uuid5(LABEL_NAMESPACE, f"{SEED_TENANT_LABEL}.{LABEL_EMAIL_DOMAIN}")


def ensure_seed_user() -> UUID:
    """Create-if-missing the corpus-seed ``users`` row; return its id."""
    user_id = resolve_tenant(SEED_TENANT_LABEL)
    if user_id != seed_user_id():
        msg = (
            f"seed-user derivation drift: resolve_tenant gave {user_id}, "
            f"seed_user_id() gives {seed_user_id()} — keep the two in lockstep"
        )
        raise RuntimeError(msg)
    return user_id


def plan_seed(
    books: Sequence[SeedBook],
    *,
    files_dir: Path,
    user_id: UUID,
) -> tuple[SeedPlanItem, ...]:
    """Classify every manifest entry: enqueueable, missing, or dup-content.

    Byte-identical files within one run share a derived task id, so only
    the first is enqueueable — the worker's content dedup would converge
    them anyway, but two broker messages under one Celery task id is a
    self-inflicted redelivery race.
    """
    items: list[SeedPlanItem] = []
    first_filename_by_sha: dict[str, str] = {}
    for book in books:
        path = files_dir / book.filename
        if not path.is_file():
            items.append(
                SeedPlanItem(book=book, path=path, sha256_hex=None, task_id=None, duplicate_of=None)
            )
            continue
        sha = file_sha256(path)
        earlier = first_filename_by_sha.get(sha)
        if earlier is not None:
            items.append(
                SeedPlanItem(
                    book=book, path=path, sha256_hex=sha, task_id=None, duplicate_of=earlier
                )
            )
            continue
        first_filename_by_sha[sha] = book.filename
        items.append(
            SeedPlanItem(
                book=book,
                path=path,
                sha256_hex=sha,
                task_id=derive_task_id(sha256_hex=sha, user_id=user_id),
                duplicate_of=None,
            )
        )
    return tuple(items)


def ensure_upload_task_row(*, task_id: UUID, user_id: UUID, filename: str) -> None:
    """Commit the ownership/idempotency row BEFORE the broker message.

    Mirrors ``api/uploads.py``'s deliberate ordering. ``ON CONFLICT DO
    NOTHING`` on the ``task_id`` PK preserves a previous run's row — and
    crucially any ``book_id`` claim recorded on it, which is what lets a
    crashed run's redelivery converge under the same ``book_id``.
    """
    sf = get_sync_session_factory()
    with sf() as session, session.begin():
        stmt = (
            pg_insert(UploadTask)
            .values(task_id=task_id, user_id=user_id, filename=filename)
            .on_conflict_do_nothing(index_elements=["task_id"])
        )
        session.execute(stmt)


def _ping_worker() -> bool:
    """True when at least one Celery worker answers within 2 s."""
    replies = ingest_book.app.control.ping(timeout=2.0)
    return bool(replies)


def _report_outcome(async_result: Any, *, timeout: float) -> str:
    """Wait for one task and print its outcome. Returns new|duplicate|failed."""
    try:
        result = async_result.get(timeout=timeout, propagate=False, interval=5.0)
    except CeleryTimeoutError:
        sys.stderr.write(
            f"    TIMEOUT after {timeout:.0f}s — task {async_result.id} may still be "
            f"running; do NOT re-run the seeder until the worker is idle.\n",
        )
        return "failed"
    if not async_result.successful():
        sys.stderr.write(f"    FAILED: {result!r}\n")
        return "failed"
    payload = cast("dict[str, object]", result)
    book_id = payload.get("book_id")
    if payload.get("was_duplicate"):
        sys.stdout.write(f"    duplicate — converged onto book_id={book_id} (0 new vectors)\n")
        return "duplicate"
    sys.stdout.write(
        f"    new book book_id={book_id} ({payload.get('rows_inserted')} vectors)\n",
    )
    return "new"


def run_seed(
    *,
    manifest_path: Path,
    files_dir: Path,
    dry_run: bool,
    max_in_flight: int,
    timeout: float,
) -> int:
    """Plan and (unless *dry_run*) enqueue the seed corpus. Returns exit code."""
    books = load_manifest(manifest_path)
    user_id = seed_user_id()
    plan = plan_seed(books, files_dir=files_dir, user_id=user_id)
    total = len(plan)
    mode = "[dry-run] " if dry_run else ""
    sys.stdout.write(
        f"{mode}Seed manifest {manifest_path} ({total} entries); files under "
        f"{files_dir}; owner {SEED_TENANT_LABEL} (user_id={user_id}).\n",
    )

    if not dry_run:
        try:
            worker_up = _ping_worker()
        except OperationalError as exc:
            sys.stderr.write(
                f"Cannot reach the Celery broker: {exc}\n"
                "Is the compose stack up (`make up` at the repo root) and infra/.env "
                "sourced? Run via `make seed-corpus`, which sources it.\n",
            )
            return _EXIT_ENV
        if not worker_up:
            sys.stderr.write(
                "No Celery worker responded to ping — start one first "
                "(`make -C worker worker` in another terminal).\n",
            )
            return _EXIT_ENV
        ensure_seed_user()

    counts = {"new": 0, "duplicate": 0, "failed": 0, "missing": 0, "dup-content": 0}
    in_flight: deque[Any] = deque()

    def drain_one() -> None:
        async_result = in_flight.popleft()
        counts[_report_outcome(async_result, timeout=timeout)] += 1

    for index, item in enumerate(plan, start=1):
        prefix = f"[{index}/{total}] {item.book.filename}"
        if not item.present:
            counts["missing"] += 1
            sys.stdout.write(
                f"{prefix}: missing — skipped. Download it per docs/SEED_CORPUS.md "
                f"({item.book.download_url}).\n",
            )
            continue
        if item.duplicate_of is not None:
            counts["dup-content"] += 1
            sys.stdout.write(
                f"{prefix}: byte-identical to {item.duplicate_of} — skipped "
                f"(content dedup would converge them; one task id must not be reused).\n",
            )
            continue
        task_id, sha = item.task_id, item.sha256_hex
        if task_id is None or sha is None:  # unreachable — plan_seed invariant
            continue
        if dry_run:
            sys.stdout.write(f"{prefix}: would enqueue task_id={task_id} sha256={sha[:12]}…\n")
            continue
        ensure_upload_task_row(task_id=task_id, user_id=user_id, filename=item.book.filename)
        # Deterministic task ids mean a PREVIOUS run's result can still sit
        # in the result backend under this id. Drop it BEFORE publishing,
        # or `.get()` below returns the stale cached payload instead of
        # THIS run's outcome — a re-run would mis-report "new book" where
        # the worker actually converged as a duplicate, and a cached
        # SUCCESS would mask a fresh failure. No-op when no result exists.
        ingest_book.AsyncResult(str(task_id)).forget()
        async_result = ingest_book.apply_async(
            args=[str(item.path), str(user_id)],
            task_id=str(task_id),
        )
        sys.stdout.write(f"{prefix}: enqueued task_id={async_result.id} sha256={sha[:12]}…\n")
        in_flight.append(async_result)
        while len(in_flight) >= max_in_flight:
            drain_one()

    while in_flight:
        drain_one()

    enqueued = counts["new"] + counts["duplicate"] + counts["failed"]
    sys.stdout.write(
        f"{mode}Summary: enqueued={enqueued} new={counts['new']} "
        f"duplicate={counts['duplicate']} failed={counts['failed']} "
        f"missing={counts['missing']} dup-content={counts['dup-content']}\n",
    )
    if counts["failed"]:
        return _EXIT_FAILED
    if not dry_run and enqueued == 0:
        sys.stderr.write(
            "Nothing was enqueued — no manifest file present under the files dir. "
            "Download the corpus per docs/SEED_CORPUS.md first.\n",
        )
        return _EXIT_FAILED
    return _EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="seed_corpus",
        description=(
            "Idempotently bulk-ingest the public-domain seed corpus through the "
            "Celery + dedup path with deterministic Phase 20 idempotency claims. "
            "Re-runs converge (content dedup + task-id claim); see docs/SEED_CORPUS.md."
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"JSONL seed manifest (default: {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--files-dir",
        type=Path,
        default=DEFAULT_FILES_DIR,
        help=(
            f"Directory holding the downloaded, legally-held ebook files "
            f"(default: {DEFAULT_FILES_DIR} — gitignored, shared with the golden corpus)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only: report present/missing files and derived task ids; no DB/broker access.",
    )
    parser.add_argument(
        "--max-in-flight",
        type=int,
        default=1,
        help=(
            "Books enqueued concurrently (default 1 = serial, the safe mode). Raise only "
            "to exactly the worker's --concurrency — see docs/SEED_CORPUS.md 'Parallelism'."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=7200.0,
        help="Per-book wait in seconds (default 7200 — CPU ingest is tens of minutes/book).",
    )
    args = parser.parse_args(argv)
    if args.max_in_flight < 1:
        parser.error("--max-in-flight must be >= 1")
    return run_seed(
        manifest_path=args.manifest,
        files_dir=args.files_dir,
        dry_run=args.dry_run,
        max_in_flight=args.max_in_flight,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
