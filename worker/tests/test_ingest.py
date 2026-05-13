"""Smoke tests for the single-book ingest pipeline.

Three layers:

1. **Pure unit** — `_build_rows` against synthetic chunks + a deterministic
   ndarray. No model load, no Milvus. Runs in CI.
2. **Argument validation** — empty `user_id` / `book_id` rejected. No
   model load, no Milvus.
3. **End-to-end (chunk → embed → insert)** — drives `ingest_markdown`
   with a tiny synthetic markdown string. Real BGE-Large, real Milvus.
   Skipped without (a) HF cache or (b) reachable Milvus.

Driving the e2e tests via `ingest_markdown` (not `ingest`) is deliberate:
semantic chunking on a novel-sized EPUB embeds every sentence-pair for
boundary detection and takes ~10 min per pass on CPU. A 5-sentence
synthetic markdown produces ~1–3 chunks and runs in <1s. The full-EPUB
pipeline is verified manually for Phase 6 acceptance — see the
`docs/PHASES.md` tick notes.
"""

# Tests reach for `_build_rows` — it's a small internal helper but the
# cheapest covered path for the metadata-shape assertions.
# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnnecessaryComparison=false

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from chunking import Chunk
from embedding import EMBED_DIM, MODEL_NAME
from ingest import _build_rows, ingest, ingest_markdown
from scripts.bootstrap_milvus import COLLECTION_NAME

TEST_BOOK_ID = "_test_phase6_ingest_"

# Five short distinct sentences across two ATX headings. SemanticSplitter
# can emit anywhere from 1 to a few chunks here; the e2e tests only assert
# `inserted > 0` and per-row metadata shape, not the exact chunk count.
SYNTHETIC_MARKDOWN = """\
# Introduction

The morning light fell across the open page. The reader paused to consider its meaning.

# Chapter One

A bell rang in the distance and the village stirred. Smoke rose from the cottages.
A child laughed somewhere down the lane.
"""


def _model_available() -> bool:
    if os.environ.get("HF_HUB_OFFLINE") == "1":
        return False
    cache = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    slug = MODEL_NAME.replace("/", "--")
    return (cache / "hub" / f"models--{slug}").is_dir()


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


def test_ingest_requires_user_id() -> None:
    with pytest.raises(ValueError, match="user_id"):
        ingest(path=Path("/dev/null"), user_id="", book_id="b_x")


def test_ingest_requires_book_id() -> None:
    with pytest.raises(ValueError, match="book_id"):
        ingest(path=Path("/dev/null"), user_id="u_a", book_id="")


def test_ingest_markdown_requires_user_id() -> None:
    with pytest.raises(ValueError, match="user_id"):
        ingest_markdown(markdown="x", filename="x.md", user_id="", book_id="b")


def test_ingest_markdown_requires_book_id() -> None:
    with pytest.raises(ValueError, match="book_id"):
        ingest_markdown(markdown="x", filename="x.md", user_id="u", book_id="")


@pytest.fixture
def milvus_clean_test_book() -> Iterator[None]:
    """Strip any leftover `_test_phase6_ingest_` rows before and after the test."""
    if not _milvus_reachable():
        yield
        return
    from pymilvus import MilvusClient

    host, port = _milvus_host_port()
    client = MilvusClient(uri=f"http://{host}:{port}")
    if client.has_collection(collection_name=COLLECTION_NAME):
        client.delete(
            collection_name=COLLECTION_NAME,
            filter=f'book_id == "{TEST_BOOK_ID}"',
        )
        client.flush(collection_name=COLLECTION_NAME)
    yield
    if client.has_collection(collection_name=COLLECTION_NAME):
        client.delete(
            collection_name=COLLECTION_NAME,
            filter=f'book_id == "{TEST_BOOK_ID}"',
        )
        client.flush(collection_name=COLLECTION_NAME)


@pytest.mark.skipif(
    not _model_available(),
    reason="BGE-Large model not in HF cache — set HF_HOME or prewarm to run",
)
def test_ingest_markdown_inserts_rows_and_rejects_dup(
    milvus_clean_test_book: None,  # noqa: ARG001 — fixture used for setup/teardown
) -> None:
    """One e2e path: insert, query back, then re-ingest must raise without --force.

    Combines the two critical invariants for ingest in one test so the
    chunk+embed cycle only runs once. The force-replace path is exercised
    by `test_ingest_markdown_force_replaces_existing` below.
    """
    if not _milvus_reachable():
        host, port = _milvus_host_port()
        pytest.skip(f"Milvus unreachable at {host}:{port}; run `make up`.")

    from pymilvus import MilvusClient

    host, port = _milvus_host_port()
    client = MilvusClient(uri=f"http://{host}:{port}")
    if not client.has_collection(collection_name=COLLECTION_NAME):
        pytest.skip(f"Collection '{COLLECTION_NAME}' missing — run `make bootstrap-milvus`.")

    inserted = ingest_markdown(
        markdown=SYNTHETIC_MARKDOWN,
        filename="synthetic.md",
        user_id="u_test_phase6",
        book_id=TEST_BOOK_ID,
        client=client,
    )
    assert inserted > 0

    rows = client.query(
        collection_name=COLLECTION_NAME,
        filter=f'book_id == "{TEST_BOOK_ID}"',
        output_fields=["book_id", "content_chunk", "metadata"],
        limit=inserted + 10,
    )
    assert len(rows) == inserted
    sample = rows[0]
    assert sample["book_id"] == TEST_BOOK_ID
    assert sample["metadata"]["filename"] == "synthetic.md"
    assert isinstance(sample["metadata"]["chunk_index"], int)

    # Re-ingest without --force must refuse.
    with pytest.raises(FileExistsError, match="Vectors already exist"):
        ingest_markdown(
            markdown=SYNTHETIC_MARKDOWN,
            filename="synthetic.md",
            user_id="u_test_phase6",
            book_id=TEST_BOOK_ID,
            client=client,
        )


@pytest.mark.skipif(
    not _model_available(),
    reason="BGE-Large model not in HF cache — set HF_HOME or prewarm to run",
)
def test_ingest_markdown_force_replaces_existing(
    milvus_clean_test_book: None,  # noqa: ARG001
) -> None:
    """force=True must delete existing rows for this book_id then re-insert."""
    if not _milvus_reachable():
        host, port = _milvus_host_port()
        pytest.skip(f"Milvus unreachable at {host}:{port}; run `make up`.")

    from pymilvus import MilvusClient

    host, port = _milvus_host_port()
    client = MilvusClient(uri=f"http://{host}:{port}")
    if not client.has_collection(collection_name=COLLECTION_NAME):
        pytest.skip(f"Collection '{COLLECTION_NAME}' missing — run `make bootstrap-milvus`.")

    first = ingest_markdown(
        markdown=SYNTHETIC_MARKDOWN,
        filename="synthetic.md",
        user_id="u_test_phase6",
        book_id=TEST_BOOK_ID,
        client=client,
    )
    assert first > 0

    replaced = ingest_markdown(
        markdown=SYNTHETIC_MARKDOWN,
        filename="synthetic.md",
        user_id="u_test_phase6",
        book_id=TEST_BOOK_ID,
        client=client,
        force=True,
    )
    assert replaced == first

    rows = client.query(
        collection_name=COLLECTION_NAME,
        filter=f'book_id == "{TEST_BOOK_ID}"',
        output_fields=["id"],
        limit=replaced + 100,
    )
    assert len(rows) == replaced
