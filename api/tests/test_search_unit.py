"""Unit tests for retrieval helpers — no DB, no Milvus, no model load.

Live retrieval is covered by ``worker/tests/test_retrieval_golden.py``;
this file pins the small, deterministic pieces of ``worker/retrieval.py``
that the API depends on so regressions in filter-expression shape,
RRF fusion math, or short-circuit semantics surface without requiring
infra.
"""

# Tests exercise module-internals on purpose. ``pytest.approx`` ships
# loose stubs that pyright strict reports as Unknown — silence per-file.
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false

from __future__ import annotations

import uuid

import pytest
from retrieval import RetrievalHit, _build_milvus_filter, rrf_fuse


def test_build_milvus_filter_quotes_each_uuid() -> None:
    a = uuid.UUID("11111111-1111-1111-1111-111111111111")
    b = uuid.UUID("22222222-2222-2222-2222-222222222222")
    expr = _build_milvus_filter([a, b])
    # Milvus expects `book_id in ["uuid1", "uuid2"]`; the exact string is
    # the contract between the API and the partition-key filter.
    expected = (
        'book_id in ["11111111-1111-1111-1111-111111111111", '
        '"22222222-2222-2222-2222-222222222222"]'
    )
    assert expr == expected


def test_build_milvus_filter_rejects_empty() -> None:
    # An empty filter list would either become `book_id in []` (which some
    # pymilvus builds reject) or — worse — an accidentally-unfiltered
    # search if the caller stripped the clause. The endpoint short-circuits
    # the request before calling this helper; we enforce that contract
    # here so a future caller can't bypass it silently.
    with pytest.raises(ValueError, match="at least one book_id"):
        _build_milvus_filter([])


def test_build_milvus_filter_single_book_id() -> None:
    bid = uuid.UUID("33333333-3333-3333-3333-333333333333")
    assert _build_milvus_filter([bid]) == 'book_id in ["33333333-3333-3333-3333-333333333333"]'


def _hit(book_id: uuid.UUID, chunk_index: int, *, score: float = 1.0) -> RetrievalHit:
    return RetrievalHit(
        book_id=book_id,
        chunk_index=chunk_index,
        content_chunk=f"chunk-{chunk_index}",
        metadata={"chunk_index": chunk_index},
        score=score,
        dense_score=score,
    )


def test_rrf_fuse_sums_both_arms_for_shared_hit() -> None:
    """A chunk present in both arms gets 1/(k+rank_dense) + 1/(k+rank_sparse)."""
    bid = uuid.UUID("44444444-4444-4444-4444-444444444444")
    # Same chunk at dense-rank-1 and sparse-rank-2.
    dense = [_hit(bid, 0, score=0.9)]
    sparse = [
        RetrievalHit(
            book_id=uuid.UUID("55555555-5555-5555-5555-555555555555"),
            chunk_index=7,
            content_chunk="other",
            metadata={"chunk_index": 7},
            score=0.4,
            sparse_score=0.4,
        ),
        RetrievalHit(
            book_id=bid,
            chunk_index=0,
            content_chunk="chunk-0",
            metadata={"chunk_index": 0},
            score=0.3,
            sparse_score=0.3,
        ),
    ]
    fused = rrf_fuse(dense=dense, sparse=sparse, limit=10, k=60)
    # Shared chunk: 1/61 (dense rank 1) + 1/62 (sparse rank 2) = 0.03251...
    # Other chunk: 1/61 (sparse rank 1)                          = 0.01639...
    assert len(fused) == 2
    assert fused[0].book_id == bid
    assert fused[0].dense_score == pytest.approx(0.9)
    assert fused[0].sparse_score == pytest.approx(0.3)
    assert fused[0].score == pytest.approx(1 / 61 + 1 / 62)
    # Solo entry from sparse arm keeps sparse_score, no dense_score.
    assert fused[1].dense_score is None
    assert fused[1].sparse_score == pytest.approx(0.4)
    assert fused[1].score == pytest.approx(1 / 61)


def test_rrf_fuse_orders_by_fused_score_desc() -> None:
    """Top hit is whichever has the highest summed reciprocal rank."""
    bid_a = uuid.UUID("66666666-6666-6666-6666-666666666666")
    bid_b = uuid.UUID("77777777-7777-7777-7777-777777777777")
    # bid_a appears only in dense at rank 1.
    # bid_b appears in both arms — beats bid_a even though bid_a is top of dense.
    dense = [_hit(bid_a, 0), _hit(bid_b, 3)]
    sparse = [
        RetrievalHit(
            book_id=bid_b,
            chunk_index=3,
            content_chunk="x",
            metadata={"chunk_index": 3},
            score=0.5,
            sparse_score=0.5,
        ),
    ]
    fused = rrf_fuse(dense=dense, sparse=sparse, limit=10, k=60)
    assert [h.book_id for h in fused] == [bid_b, bid_a]


def test_rrf_fuse_respects_limit() -> None:
    bids = [uuid.uuid4() for _ in range(5)]
    dense = [_hit(b, i) for i, b in enumerate(bids)]
    sparse: list[RetrievalHit] = []
    fused = rrf_fuse(dense=dense, sparse=sparse, limit=3, k=60)
    assert len(fused) == 3
    # Still ordered by RRF (which mirrors dense order when sparse is empty).
    assert [h.book_id for h in fused] == bids[:3]


def test_rrf_fuse_empty_both_arms_returns_empty() -> None:
    assert rrf_fuse(dense=[], sparse=[], limit=10, k=60) == []
