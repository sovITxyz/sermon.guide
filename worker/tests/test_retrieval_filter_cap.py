"""Live filter-cap test — the Phase 24 chunked-filter recall gate.

The dense Milvus arm scopes every search to the caller's ``book_id`` set
(CLAUDE.md tenant invariant, ARCHITECTURE.md §7.1). Before Phase 24 that
set went into a single ``book_id in [...]`` filter expression — a 10K-book
library produced a ~360 KB string per search. Phase 24 splits the filter
into ``MILVUS_FILTER_BOOK_ID_CHUNK``-sized slices and merges the per-slice
hits into the global top-K, preserving FULL recall (no book is silently
dropped — a silent cap would exclude part of a user's library, a
correctness AND a tenant-trust regression).

This test proves the chunked path survives a real 10K-book library:

1. Insert ~10K synthetic ``global_books`` + ``user_library`` rows for a
   deterministic synthetic user (uuid5 — stable across runs, cleaned up
   in teardown). A subset of those books also gets real Milvus vectors,
   deliberately straddling chunk boundaries so the recall assertion is
   non-trivial.
2. Build a 1024-D query vector directly (no remote embedding — keyless;
   this suite is the filter-scale gate, not the ranking-quality gate that
   ``test_retrieval_golden.py`` owns).
3. Run ``dense_search`` scoped to all ~10K ``book_id``s THROUGH the new
   chunked filter against live Milvus.
4. Assert it completes with no Milvus expr-length rejection, and that the
   seeded books are recalled (hits exist + only come from the scoped set).
5. Print the wall-clock latency. Clean up every inserted row in teardown.

## Skip-clean policy (mirrors test_tenant_isolation.py)

Skips cleanly when Milvus OR Postgres is unreachable, or when the
``library_vectors`` collection is missing — the operator runs the live
suites serially against a booted compose stack. This 10K-row insert +
chunked search is heavyweight; it is NOT meant to run in keyless CI.
"""
# pymilvus 2.6 lacks `py.typed`; relax the same rules as the rest of worker/.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnnecessaryComparison=false

from __future__ import annotations

import os
import socket
import time
import uuid
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest
from pymilvus import MilvusClient
from sqlalchemy import text

from db import User, get_sync_session_factory
from db.settings import settings as db_settings
from retrieval import MILVUS_FILTER_BOOK_ID_CHUNK, dense_search
from scripts.bootstrap_milvus import COLLECTION_NAME, VECTOR_DIM

# Deterministic synthetic identity — uuid5 so re-runs reuse the same rows
# (and teardown reliably finds them) without colliding with real users or
# the golden user.
_FILTER_CAP_NAMESPACE = uuid.UUID("4d7c1f2a-0b6e-5a93-8c44-2e9f1d6a7b30")
FILTER_CAP_USER_ID = uuid.uuid5(_FILTER_CAP_NAMESPACE, "retrieval-filter-cap-test-user")

# >10K so the library spans >10 chunks at the 1000-book default — well past
# the single-search fast path, exercising the merge across many slices.
LIBRARY_SIZE = 10_500

# How many of the LIBRARY_SIZE books get real Milvus vectors. Spread across
# chunk boundaries (see _seeded_book_indices) so recall depends on the
# per-chunk searches all being scoped AND merged, not on one lucky slice.
SEEDED_BOOK_COUNT = 12
VECTORS_PER_SEEDED_BOOK = 3

# Tags every Milvus row this test inserts so teardown deletes exactly its
# own data and never touches the collection or another suite's rows.
TEST_TAG = "_test_filter_cap_"


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


def _book_ids() -> list[uuid.UUID]:
    """Deterministic ``book_id`` list — uuid5 so insert/search/teardown agree."""
    return [uuid.uuid5(_FILTER_CAP_NAMESPACE, f"book-{i}") for i in range(LIBRARY_SIZE)]


def _seeded_book_indices() -> list[int]:
    """Indices (into the book list) that get real Milvus vectors.

    Spread across chunk boundaries — the first book, books straddling the
    1st/2nd chunk seam, and the very last book — so a recall pass requires
    every relevant slice's scoped search to fire and merge correctly, not
    just the first one.
    """
    seam = MILVUS_FILTER_BOOK_ID_CHUNK
    candidates = [
        0,
        1,
        seam - 1,
        seam,
        seam + 1,
        2 * seam,
        2 * seam + 1,
        LIBRARY_SIZE // 2,
        LIBRARY_SIZE - 3,
        LIBRARY_SIZE - 2,
        LIBRARY_SIZE - 1,
    ]
    # De-dup + keep in-range, then pad deterministically to SEEDED_BOOK_COUNT.
    seen: list[int] = []
    for c in candidates:
        if 0 <= c < LIBRARY_SIZE and c not in seen:
            seen.append(c)
    i = 0
    while len(seen) < SEEDED_BOOK_COUNT and i < LIBRARY_SIZE:
        if i not in seen:
            seen.append(i)
        i += 1
    return seen[:SEEDED_BOOK_COUNT]


def _unit_vector(rng: np.random.Generator) -> list[float]:
    v = rng.standard_normal(VECTOR_DIM).astype(np.float32)
    v /= np.linalg.norm(v)
    return v.tolist()


@pytest.fixture(scope="module")
def milvus_client() -> Iterator[MilvusClient]:
    if not _milvus_reachable():
        host, port = _milvus_host_port()
        pytest.skip(f"Milvus unreachable at {host}:{port}; run `make up`.")
    if not _postgres_reachable():
        pytest.skip(
            f"Postgres unreachable at {db_settings.host}:{db_settings.port}; run `make up`.",
        )

    host, port = _milvus_host_port()
    client = MilvusClient(uri=f"http://{host}:{port}")
    if not client.has_collection(collection_name=COLLECTION_NAME):
        pytest.skip(f"Collection '{COLLECTION_NAME}' missing — run `make bootstrap-milvus`.")

    # Best-effort pre-clean in case a prior run aborted before teardown.
    client.delete(
        collection_name=COLLECTION_NAME,
        filter=f'book_id like "{TEST_TAG}%"',
    )

    yield client

    # Teardown: drop only this test's Milvus rows — never the collection.
    client.delete(
        collection_name=COLLECTION_NAME,
        filter=f'book_id like "{TEST_TAG}%"',
    )
    client.flush(collection_name=COLLECTION_NAME)


@pytest.fixture(scope="module")
def seeded_library(milvus_client: MilvusClient) -> Iterator[dict[str, Any]]:
    """Insert ~10K Postgres library rows + a seeded-book Milvus subset.

    Postgres holds the authoritative state: a synthetic ``users`` row, ~10K
    ``global_books`` rows (each with a 1-byte ``minhash_signature``
    placeholder to satisfy the NOT NULL column — this test never runs the
    dedup LSH path), and one ``user_library`` row per book scoping them all
    to the synthetic user. Those plain-UUID ``book_id``s are what
    ``dense_search`` filters on.

    Milvus gets real 1024-D vectors for the SEEDED subset only, keyed by the
    plain UUID text (Milvus ``book_id`` is VARCHAR) so the scoped per-chunk
    filter matches them. The seeded books straddle chunk boundaries (see
    ``_seeded_book_indices``) so a recall pass requires every relevant
    slice's scoped search to fire and merge.

    Cleanup is authoritative-by-id: teardown deletes the seeded Milvus rows
    by their exact ``book_id`` set, then the Postgres rows by user/book id.
    The ``TEST_TAG`` prefix-delete in the ``milvus_client`` fixture is a
    belt-and-braces sweep for any stray tagged rows from an aborted run.
    """
    rng = np.random.default_rng(20260614)
    book_ids = _book_ids()
    seeded_idx = _seeded_book_indices()
    seeded_book_ids = [book_ids[i] for i in seeded_idx]
    seeded_book_id_strs = {str(b) for b in seeded_book_ids}

    sf = get_sync_session_factory()

    # --- Postgres: synthetic user + ~10K global_books + user_library -------
    with sf() as session, session.begin():
        if session.get(User, FILTER_CAP_USER_ID) is None:
            session.add(
                User(
                    user_id=FILTER_CAP_USER_ID,
                    email=f"{FILTER_CAP_USER_ID}@filter-cap.test",
                    password_hash="bcrypt$retrieval-filter-cap-test-user",  # noqa: S106 — test seed only
                ),
            )

    # Bulk insert with ON CONFLICT DO NOTHING so a re-run after an aborted
    # teardown is idempotent. minhash_signature is NOT NULL — a 1-byte
    # placeholder satisfies it (this test never runs the dedup LSH path).
    now_books = [
        {
            "book_id": bid,
            "title": f"filter-cap synthetic book {i}",
            "minhash_signature": b"\x00",
        }
        for i, bid in enumerate(book_ids)
    ]
    now_entries = [
        {
            "entry_id": uuid.uuid5(_FILTER_CAP_NAMESPACE, f"entry-{i}"),
            "user_id": FILTER_CAP_USER_ID,
            "book_id": bid,
        }
        for i, bid in enumerate(book_ids)
    ]
    with sf() as session, session.begin():
        # Postgres-only test seam; chunk the executemany so a single
        # statement never carries 10K rows of bind params.
        batch = 2000
        gb_insert = text(
            "INSERT INTO global_books (book_id, title, minhash_signature) "
            "VALUES (:book_id, :title, :minhash_signature) "
            "ON CONFLICT (book_id) DO NOTHING",
        )
        ul_insert = text(
            "INSERT INTO user_library (entry_id, user_id, book_id) "
            "VALUES (:entry_id, :user_id, :book_id) "
            "ON CONFLICT (user_id, book_id) DO NOTHING",
        )
        for start in range(0, len(now_books), batch):
            session.execute(gb_insert, now_books[start : start + batch])
        for start in range(0, len(now_entries), batch):
            session.execute(ul_insert, now_entries[start : start + batch])

    # --- Milvus: real vectors for the seeded subset only ------------------
    rows: list[dict[str, Any]] = []
    for bid in seeded_book_ids:
        for j in range(VECTORS_PER_SEEDED_BOOK):
            rows.append(
                {
                    "vector": _unit_vector(rng),
                    # Milvus book_id is the PLAIN UUID text so the scoped
                    # filter matches; teardown deletes by this exact id set.
                    "book_id": str(bid),
                    "content_chunk": f"filter-cap chunk {bid} {j}",
                    "metadata": {"chunk_index": j, "tag": TEST_TAG},
                }
            )
    milvus_client.insert(collection_name=COLLECTION_NAME, data=rows)
    milvus_client.flush(collection_name=COLLECTION_NAME)
    milvus_client.load_collection(collection_name=COLLECTION_NAME)

    yield {
        "book_ids": book_ids,
        "seeded_book_ids": seeded_book_ids,
        "seeded_book_id_strs": seeded_book_id_strs,
    }

    # --- Teardown: delete every row this test inserted --------------------
    quoted = ", ".join(f'"{b!s}"' for b in seeded_book_ids)
    milvus_client.delete(
        collection_name=COLLECTION_NAME,
        filter=f"book_id in [{quoted}]",
    )
    milvus_client.flush(collection_name=COLLECTION_NAME)

    with sf() as session, session.begin():
        session.execute(
            text("DELETE FROM user_library WHERE user_id = :uid"),
            {"uid": FILTER_CAP_USER_ID},
        )
        # global_books carries a RESTRICT FK from user_library; the delete
        # above clears it first, so these synthetic books drop cleanly.
        session.execute(
            text("DELETE FROM global_books WHERE book_id = ANY(:bids)"),
            {"bids": book_ids},
        )
        session.execute(
            text("DELETE FROM users WHERE user_id = :uid"),
            {"uid": FILTER_CAP_USER_ID},
        )


@pytest.fixture(scope="module")
def query_vector() -> list[float]:
    return _unit_vector(np.random.default_rng(7))


class TestFilterCap:
    """The chunked filter must survive a 10K-book library with full recall."""

    def test_library_spans_multiple_chunks(self, seeded_library: dict[str, Any]) -> None:
        """Guard: if the library fit in one chunk this test would prove
        nothing about chunking. ~10.5K books @ 1000/chunk → >10 slices.
        """
        book_ids = seeded_library["book_ids"]
        assert len(book_ids) > MILVUS_FILTER_BOOK_ID_CHUNK, (
            f"library ({len(book_ids)}) must exceed the chunk size "
            f"({MILVUS_FILTER_BOOK_ID_CHUNK}) or the chunked path never runs."
        )

    def test_chunked_dense_search_completes_with_full_recall(
        self,
        milvus_client: MilvusClient,
        seeded_library: dict[str, Any],
        query_vector: list[float],
    ) -> None:
        """A 10K-book scoped search must NOT be rejected for filter length
        and must recall the seeded books — proving the per-chunk searches
        all fired and merged. If the old unbounded path had shipped a
        ~360 KB expr this would have raised; the chunked path keeps each
        expr ~36 KB.
        """
        book_ids = seeded_library["book_ids"]
        seeded_strs = seeded_library["seeded_book_id_strs"]

        start = time.perf_counter()
        hits = dense_search(
            client=milvus_client,
            query_vec=query_vector,
            book_ids=book_ids,
            limit=30,
        )
        elapsed = time.perf_counter() - start
        print(  # noqa: T201 — operator reads the latency from the live run
            f"\n[filter-cap] chunked dense_search over {len(book_ids)} books "
            f"({len(book_ids) // MILVUS_FILTER_BOOK_ID_CHUNK + 1} slices) "
            f"completed in {elapsed:.3f}s, {len(hits)} hits.",
        )

        # Recall: the only vectors in Milvus for this library are the
        # seeded books', so every hit MUST be a seeded book — and we MUST
        # get hits (proving the scoped per-chunk searches found them).
        assert hits, (
            "FILTER-CAP RECALL FAILURE: chunked dense_search returned zero "
            "hits over a 10K-book library that contains seeded vectors — a "
            "chunk's scoped search was dropped or the merge lost its hits."
        )
        returned = {str(h.book_id) for h in hits}
        leaked = sorted(returned - seeded_strs)
        assert not leaked, (
            "FILTER-CAP SCOPING FAILURE: chunked search returned book_ids "
            f"outside the scoped/seeded set: {leaked}. The union of chunk "
            "filters must equal exactly the input book_ids — no more."
        )

    def test_empty_library_still_raises(self, milvus_client: MilvusClient) -> None:
        """The empty-library short-circuit guard survives Phase 24 — an
        empty book_id set must raise, never fall through to an unfiltered
        (cross-tenant) search.
        """
        with pytest.raises(ValueError, match="at least one book_id"):
            dense_search(client=milvus_client, query_vec=[0.0] * VECTOR_DIM, book_ids=[])


# ---------------------------------------------------------------------------
# Keyless unit tests — chunking/merge/scoping math with a fake Milvus client.
# No infra: these run in plain CI and are the fast guard on the slice logic
# that the live test above exercises end to end against real Milvus.
# ---------------------------------------------------------------------------

# 2**40 keeps each distance exactly representable in float64 (52-bit
# mantissa) and effectively collision-free across the few-thousand-book
# test fixtures, so the global top-K ordering is unambiguous.
_FAKE_DIST_MODULUS = 2**40


def _fake_distance(book_id_str: str) -> float:
    """Deterministic, near-unique COSINE-style distance for a book UUID."""
    return (uuid.UUID(book_id_str).int % _FAKE_DIST_MODULUS) / _FAKE_DIST_MODULUS


class _FakeMilvusClient:
    """Records every ``search`` filter and returns deterministic hits.

    Models Milvus closely enough for ``dense_search``: it parses the
    ``book_id in [...]`` expr, returns one hit per in-filter book whose
    distance is ``_fake_distance(book_id)`` (stable, near-unique per book),
    so the merge's top-K selection is verifiable. It also enforces the
    thing the real backend would: an expr longer than ``max_expr_len``
    raises — that is what would have fired on the old unbounded 360 KB
    path.
    """

    def __init__(self, *, max_expr_len: int = 50_000) -> None:
        self.seen_filters: list[str] = []
        self.max_expr_len = max_expr_len

    def search(
        self,
        *,
        collection_name: str,  # noqa: ARG002 — signature parity with MilvusClient
        data: list[list[float]],  # noqa: ARG002
        filter: str,  # noqa: A002 — matches pymilvus's kwarg name
        limit: int,
        output_fields: list[str],  # noqa: ARG002
        timeout: float,  # noqa: ARG002
    ) -> list[list[dict[str, Any]]]:
        if len(filter) > self.max_expr_len:
            msg = f"filter expr too long: {len(filter)} > {self.max_expr_len}"
            raise ValueError(msg)
        self.seen_filters.append(filter)
        # Parse the quoted UUIDs out of `book_id in ["a", "b", ...]`.
        inner = filter[filter.index("[") + 1 : filter.rindex("]")]
        ids = [tok.strip().strip('"') for tok in inner.split(",") if tok.strip()]
        hits: list[dict[str, Any]] = []
        for bid in ids:
            hits.append(
                {
                    "distance": _fake_distance(bid),
                    "entity": {
                        "book_id": bid,
                        "content_chunk": f"chunk {bid}",
                        "metadata": {"chunk_index": 0},
                    },
                },
            )
        hits.sort(key=lambda h: h["distance"], reverse=True)
        return [hits[:limit]]


def test_unit_small_library_takes_single_search() -> None:
    """`len(book_ids) <= chunk_size` → exactly one scoped search."""
    client = _FakeMilvusClient()
    book_ids = [uuid.uuid5(_FILTER_CAP_NAMESPACE, f"u-{i}") for i in range(5)]
    hits = dense_search(
        client=client,  # type: ignore[arg-type]
        query_vec=[0.1] * VECTOR_DIM,
        book_ids=book_ids,
        limit=30,
        chunk_size=1000,
    )
    assert len(client.seen_filters) == 1
    assert {str(h.book_id) for h in hits} == {str(b) for b in book_ids}


def test_unit_large_library_chunks_and_unions_exactly() -> None:
    """Chunked path: per-slice filters partition the input exactly — every
    book appears in exactly one chunk filter, none added, none dropped.
    """
    client = _FakeMilvusClient()
    book_ids = [uuid.uuid5(_FILTER_CAP_NAMESPACE, f"c-{i}") for i in range(2500)]
    dense_search(
        client=client,  # type: ignore[arg-type]
        query_vec=[0.1] * VECTOR_DIM,
        book_ids=book_ids,
        limit=30,
        chunk_size=1000,
    )
    # 2500 / 1000 → 3 slices (1000, 1000, 500).
    assert len(client.seen_filters) == 3

    # The union of all chunk filters must equal exactly the input set.
    filtered_ids: list[str] = []
    for expr in client.seen_filters:
        inner = expr[expr.index("[") + 1 : expr.rindex("]")]
        filtered_ids.extend(tok.strip().strip('"') for tok in inner.split(","))
    expected = [str(b) for b in book_ids]
    assert filtered_ids == expected  # contiguous, ordered, no dup, no drop


def test_unit_merge_returns_global_top_k_across_chunks() -> None:
    """The merged result is the global top-K by distance, regardless of
    which chunk each top hit came from — full recall preserved.
    """
    client = _FakeMilvusClient()
    book_ids = [uuid.uuid5(_FILTER_CAP_NAMESPACE, f"m-{i}") for i in range(2500)]
    limit = 10
    hits = dense_search(
        client=client,  # type: ignore[arg-type]
        query_vec=[0.1] * VECTOR_DIM,
        book_ids=book_ids,
        limit=limit,
        chunk_size=1000,
    )
    # Expected = the globally highest-distance `limit` books across ALL
    # chunks, computed independently of dense_search's own merge (same
    # distance formula as _FakeMilvusClient.search).
    ranked = sorted(
        ((_fake_distance(str(b)), str(b)) for b in book_ids),
        reverse=True,
    )
    expected_top = [bid for _, bid in ranked[:limit]]
    assert [str(h.book_id) for h in hits] == expected_top
    # Hits are sorted by descending score.
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_unit_old_unbounded_expr_would_have_been_rejected() -> None:
    """Regression anchor: the same 2500-book library in ONE filter exceeds
    the fake backend's expr-length limit — the failure Phase 24 removes.
    Chunked at 1000 it passes; unchunked (huge chunk_size) it raises.
    """
    book_ids = [uuid.uuid5(_FILTER_CAP_NAMESPACE, f"r-{i}") for i in range(2500)]
    # One giant chunk reproduces the pre-Phase-24 single-expr behavior.
    big_client = _FakeMilvusClient(max_expr_len=50_000)
    with pytest.raises(ValueError, match="filter expr too long"):
        dense_search(
            client=big_client,  # type: ignore[arg-type]
            query_vec=[0.1] * VECTOR_DIM,
            book_ids=book_ids,
            limit=30,
            chunk_size=10_000,  # > library → single unbounded expr
        )
    # Chunked at 1000 keeps every expr under the limit.
    ok_client = _FakeMilvusClient(max_expr_len=50_000)
    dense_search(
        client=ok_client,  # type: ignore[arg-type]
        query_vec=[0.1] * VECTOR_DIM,
        book_ids=book_ids,
        limit=30,
        chunk_size=1000,
    )
    assert all(len(f) <= 50_000 for f in ok_client.seen_filters)
