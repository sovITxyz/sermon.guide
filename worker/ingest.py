"""End-to-end single-book ingest CLI: file → chunks → embeddings → Milvus.

Pipeline (ARCHITECTURE.md §5 upload path, pre-Phase-8/9):

    detect (libmagic)
        → extract (EbookLib+pandoc or pymupdf4llm)
        → chunk (LlamaIndex SemanticSplitter)
        → embed (BGE-Large, L2-normalized)
        → insert with metadata JSON, partitioned by `book_id`

## Tenant scoping

Vectors are **shared globally per book** — there is no `tenant_id` on the
row (ARCHITECTURE.md §3 + §7.1). This CLI does NOT write to the
`user_library` table; that table doesn't exist yet (Phase 7). The
`--user-id` flag is required so the calling contract is stable from this
phase forward, but the row insertion is deferred. A reminder is printed
when the ingest finishes.

`user_id` is NOT placed in the vector's metadata — that would defeat the
dedup story (a single deduped book serves every owner). All tenant
scoping happens at the API layer at search time via
`book_id IN (<user's library>)` (see `.claude/agents/tenant-auditor.md`).

## Idempotency

Re-ingesting the same `book_id` is refused unless `--force` is passed.
With `--force`, existing vectors for that `book_id` are deleted before
the new ones land. This is a stopgap until Phase 8's MinHash LSH dedup
makes "same book → same row set" a property of the pipeline itself.

CLI (run from `worker/`):

    uv run python -m ingest path/to/book.epub --user-id u_alice --book-id b_pilgrim
"""

# pymilvus 2.6 ships without `py.typed` and mis-annotates a few sync methods
# as returning coroutines; relax the same rules as bootstrap_milvus.py.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnnecessaryComparison=false

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import numpy as np
from pymilvus import MilvusClient

from chunking import Chunk, chunk
from embedding import embed
from extractors import extract
from scripts.bootstrap_milvus import COLLECTION_NAME, make_client


def _build_rows(
    *,
    filename: str,
    chunks: list[Chunk],
    embeddings: np.ndarray,
    book_id: str,
) -> list[dict[str, Any]]:
    """Zip *chunks* and *embeddings* into Milvus row dicts.

    Metadata follows ARCHITECTURE.md §3:
    `{filename, chunk_index, parent_section}`. `page` is omitted — the
    Phase 5 chunker doesn't carry page anchors and the field is documented
    as optional.
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


def _book_has_vectors(client: MilvusClient, book_id: str) -> bool:
    """Return True if any row already exists for *book_id*."""
    # Escape embedded quotes defensively — book_id is operator-supplied. A
    # quote in the value would otherwise terminate the filter literal and
    # the rest of the string would be parsed as Milvus expression syntax.
    safe = book_id.replace('"', '\\"')
    existing = client.query(
        collection_name=COLLECTION_NAME,
        filter=f'book_id == "{safe}"',
        limit=1,
        output_fields=["id"],
    )
    return len(existing) > 0


def ingest_markdown(
    *,
    markdown: str,
    filename: str,
    user_id: str,
    book_id: str,
    client: MilvusClient | None = None,
    force: bool = False,
) -> int:
    """Chunk → embed → insert for already-extracted markdown.

    Split out from `ingest` so tests can drive the chunk/embed/insert seam
    with a tiny synthetic document — semantic chunking on a novel-sized
    EPUB takes ~10 min per pass on CPU and would make the suite unusable.

    Raises:
        ValueError: empty `user_id` or `book_id`.
        FileExistsError: vectors already present for `book_id` and not `force`.
    """
    if not user_id:
        msg = "user_id is required (Phase 7 will wire it to user_library)."
        raise ValueError(msg)
    if not book_id:
        msg = "book_id is required (Milvus partition key per ARCHITECTURE.md §3)."
        raise ValueError(msg)

    client = client if client is not None else make_client()

    if _book_has_vectors(client, book_id):
        if not force:
            msg = (
                f"Vectors already exist for book_id={book_id!r}; "
                "pass --force to delete and re-ingest."
            )
            raise FileExistsError(msg)
        safe = book_id.replace('"', '\\"')
        client.delete(
            collection_name=COLLECTION_NAME,
            filter=f'book_id == "{safe}"',
        )
        client.flush(collection_name=COLLECTION_NAME)

    chunks = chunk(markdown)
    if not chunks:
        return 0

    embeddings = embed([c.text for c in chunks])
    rows = _build_rows(
        filename=filename,
        chunks=chunks,
        embeddings=embeddings,
        book_id=book_id,
    )
    client.insert(collection_name=COLLECTION_NAME, data=rows)
    client.flush(collection_name=COLLECTION_NAME)
    client.load_collection(collection_name=COLLECTION_NAME)
    return len(rows)


def ingest(
    *,
    path: Path,
    user_id: str,
    book_id: str,
    client: MilvusClient | None = None,
    force: bool = False,
) -> int:
    """Run the full ingest pipeline. Returns the number of rows inserted.

    Validates `user_id` / `book_id` BEFORE invoking `extract` so a bad
    invocation surfaces as `ValueError` rather than blowing up downstream
    on a missing/wrong-type file.
    """
    if not user_id:
        msg = "user_id is required (Phase 7 will wire it to user_library)."
        raise ValueError(msg)
    if not book_id:
        msg = "book_id is required (Milvus partition key per ARCHITECTURE.md §3)."
        raise ValueError(msg)
    return ingest_markdown(
        markdown=extract(path),
        filename=path.name,
        user_id=user_id,
        book_id=book_id,
        client=client,
        force=force,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ingest",
        description=(
            "Single-book ingest: detect → extract → chunk → embed → "
            "insert into Milvus. No dedup (Phase 8), no Celery (Phase 9)."
        ),
    )
    parser.add_argument("path", type=Path, help="Path to an EPUB or PDF.")
    parser.add_argument(
        "--user-id",
        required=True,
        help="Owning user — recorded in user_library starting Phase 7.",
    )
    parser.add_argument(
        "--book-id",
        required=True,
        help="Milvus partition key for this book (ARCHITECTURE.md §7.1).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete any existing vectors for this book_id before inserting.",
    )
    args = parser.parse_args(argv)

    inserted = ingest(
        path=args.path,
        user_id=args.user_id,
        book_id=args.book_id,
        force=args.force,
    )
    sys.stdout.write(
        f"Inserted {inserted} row(s) into '{COLLECTION_NAME}' for book_id={args.book_id!r}.\n"
    )
    sys.stdout.write(f"NOTE: user_library row for user_id={args.user_id!r} deferred to Phase 7.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
