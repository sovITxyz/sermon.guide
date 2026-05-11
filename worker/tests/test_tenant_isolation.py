"""Multi-tenant isolation smoke tests — the Phase 3 hard gate.

Vectors are shared globally per deduped book and partitioned on `book_id`
(see ARCHITECTURE.md §3 and §7.1). Tenant scoping lives at the API: every
Milvus search MUST pass `book_id IN (<user's library>)` as the filter
expression. If that filter pushdown silently regresses, an unfiltered search
returns every book on the platform — a CVE-class data leak.

These tests simulate two tenants as two disjoint `book_id` sets and assert
that a filtered search for one set never returns rows from the other.

Skips cleanly when Milvus is unreachable (e.g. CI without docker-compose) —
the local `make test-isolation` target and the `/test-isolation` skill are
the load-bearing enforcement.
"""
# pymilvus 2.6 lacks `py.typed`; relax the same rules as bootstrap_milvus.py.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnnecessaryComparison=false

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest
from pymilvus import MilvusClient

COLLECTION_NAME = "library_vectors"
VECTOR_DIM = 1024
TEST_BOOK_PREFIX = "_test_isol_"

BOOKS_A: list[str] = [f"{TEST_BOOK_PREFIX}A_{i}" for i in range(5)]
BOOKS_B: list[str] = [f"{TEST_BOOK_PREFIX}B_{i}" for i in range(5)]
VECTORS_PER_BOOK = 20  # 5 books × 20 = 100 per tenant


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


def _filter_book_id_in(books: list[str]) -> str:
    quoted = ", ".join(f'"{b}"' for b in books)
    return f"book_id in [{quoted}]"


def _unit_vector(rng: np.random.Generator) -> list[float]:
    v = rng.standard_normal(VECTOR_DIM).astype(np.float32)
    v /= np.linalg.norm(v)
    return v.tolist()


@pytest.fixture(scope="module")
def milvus_client() -> Iterator[MilvusClient]:
    if not _milvus_reachable():
        host, port = _milvus_host_port()
        pytest.skip(
            f"Milvus unreachable at {host}:{port}; run `make up`. "
            "Local `make test-isolation` is the enforcement point — "
            "see ARCHITECTURE.md §7.1."
        )

    host, port = _milvus_host_port()
    client = MilvusClient(uri=f"http://{host}:{port}")
    if not client.has_collection(collection_name=COLLECTION_NAME):
        pytest.skip(f"Collection '{COLLECTION_NAME}' missing — run `make bootstrap-milvus`.")

    # Best-effort pre-clean in case a prior run aborted before teardown.
    client.delete(
        collection_name=COLLECTION_NAME,
        filter=f'book_id like "{TEST_BOOK_PREFIX}%"',
    )

    yield client

    # Teardown: drop test data only — never the collection.
    client.delete(
        collection_name=COLLECTION_NAME,
        filter=f'book_id like "{TEST_BOOK_PREFIX}%"',
    )
    client.flush(collection_name=COLLECTION_NAME)


@pytest.fixture(scope="module")
def seeded_vectors(milvus_client: MilvusClient) -> dict[str, list[str]]:
    """Insert 100 vectors with book_id ∈ A, 100 with book_id ∈ B. Seeded RNG."""
    rng = np.random.default_rng(20260511)

    rows: list[dict[str, Any]] = []
    for tenant, books in (("A", BOOKS_A), ("B", BOOKS_B)):
        for book_id in books:
            for i in range(VECTORS_PER_BOOK):
                rows.append(
                    {
                        "vector": _unit_vector(rng),
                        "book_id": book_id,
                        "content_chunk": f"tenant {tenant} chunk {book_id} {i}",
                        "metadata": {"tenant": tenant, "i": i},
                    }
                )

    milvus_client.insert(collection_name=COLLECTION_NAME, data=rows)
    milvus_client.flush(collection_name=COLLECTION_NAME)
    milvus_client.load_collection(collection_name=COLLECTION_NAME)

    return {"A": BOOKS_A, "B": BOOKS_B}


@pytest.fixture(scope="module")
def query_vector() -> list[float]:
    return _unit_vector(np.random.default_rng(99))


class TestTenantIsolation:
    """Filter pushdown is the only thing keeping tenants out of each other's data.

    See module docstring + ARCHITECTURE.md §7.1.
    """

    def test_tenant_a_query_excludes_tenant_b_books(
        self,
        milvus_client: MilvusClient,
        seeded_vectors: dict[str, list[str]],
        query_vector: list[float],
    ) -> None:
        """If this fails, tenant A's library queries are returning tenant B's
        vectors — every user can read every other user's library. CVE-class
        data leak. Do not bypass.
        """
        books_a = seeded_vectors["A"]
        books_b_set = set(seeded_vectors["B"])

        expr = _filter_book_id_in(books_a)
        results = milvus_client.search(
            collection_name=COLLECTION_NAME,
            data=[query_vector],
            filter=expr,
            limit=200,
            output_fields=["book_id"],
        )

        hit_book_ids = [hit["entity"]["book_id"] for hit in results[0]]
        leaked = sorted({b for b in hit_book_ids if b in books_b_set})
        assert not leaked, (
            "TENANT ISOLATION FAILURE: filtered search for tenant A returned "
            f"tenant B book_ids: {leaked}. CVE-class data leak — see "
            "ARCHITECTURE.md §7.1."
        )

    def test_tenant_b_query_excludes_tenant_a_books(
        self,
        milvus_client: MilvusClient,
        seeded_vectors: dict[str, list[str]],
        query_vector: list[float],
    ) -> None:
        """If this fails, tenant B's library queries are returning tenant A's
        vectors — every user can read every other user's library. CVE-class
        data leak. Do not bypass.
        """
        books_b = seeded_vectors["B"]
        books_a_set = set(seeded_vectors["A"])

        expr = _filter_book_id_in(books_b)
        results = milvus_client.search(
            collection_name=COLLECTION_NAME,
            data=[query_vector],
            filter=expr,
            limit=200,
            output_fields=["book_id"],
        )

        hit_book_ids = [hit["entity"]["book_id"] for hit in results[0]]
        leaked = sorted({b for b in hit_book_ids if b in books_a_set})
        assert not leaked, (
            "TENANT ISOLATION FAILURE: filtered search for tenant B returned "
            f"tenant A book_ids: {leaked}. CVE-class data leak — see "
            "ARCHITECTURE.md §7.1."
        )

    def test_unfiltered_search_returns_mixed(
        self,
        milvus_client: MilvusClient,
        seeded_vectors: dict[str, list[str]],
        query_vector: list[float],
    ) -> None:
        """Sanity gate: with no filter, results must contain both A and B
        book_ids. If this fails, the test data isn't actually in Milvus and
        the two tenant tests above would be false greens — they'd pass
        because there's nothing to leak, not because the filter works.
        """
        books_a_set = set(seeded_vectors["A"])
        books_b_set = set(seeded_vectors["B"])

        results = milvus_client.search(
            collection_name=COLLECTION_NAME,
            data=[query_vector],
            limit=200,
            output_fields=["book_id"],
        )
        hit_book_ids = {hit["entity"]["book_id"] for hit in results[0]}

        assert hit_book_ids & books_a_set, (
            "SANITY FAILURE: unfiltered search returned zero tenant A book_ids "
            "from the seeded set — test data missing; tenant isolation tests "
            "are false greens."
        )
        assert hit_book_ids & books_b_set, (
            "SANITY FAILURE: unfiltered search returned zero tenant B book_ids "
            "from the seeded set — test data missing; tenant isolation tests "
            "are false greens."
        )
