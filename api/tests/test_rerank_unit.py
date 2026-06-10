"""Unit tests for the reranker glue (Phase 13; remote seam since Phase 16b).

Live reranker inference is covered by the retrieval goldens (and manual
e2e against the 5-book corpus); this file pins the deterministic glue
around the remote call:

- Empty input short-circuits without any remote call.
- Top-N truncation respects the requested cap.
- The score replacement preserves RRF as ``metadata["rrf_score"]``.
- Per-arm ``dense_score`` / ``sparse_score`` survive reranking.
- Stable ordering on tied scores (RRF/input order is the tiebreak).
- The scorer receives the query plus the documents in input order.

We monkeypatch ``rerank._score_pairs`` — the seam in front of
``worker/inference.py:rerank_scores`` — so no key, no network. Every
behavioral pin from the in-process cross-encoder era survives the
Phase 16b seam swap unchanged.
"""

# Tests reach into module internals on purpose. pytest.approx ships
# loose stubs that pyright strict reports as Unknown — silence per-file.
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import uuid

import pytest
from retrieval import RetrievalHit

import rerank as rerank_module


class _FakeScorer:
    """In-process stand-in for ``rerank._score_pairs``.

    Returns a pre-baked score list so each test can drive any ordering
    it wants without touching the network. ``calls`` records every
    ``(query, documents)`` invocation for assertions about call elision
    on empty inputs and about argument order.
    """

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, query: str, documents: list[str]) -> list[float]:
        self.calls.append((query, list(documents)))
        return self._scores[: len(documents)]


def _hit(book_idx: int, chunk_index: int, *, score: float, content: str = "") -> RetrievalHit:
    """Build a synthetic ``RetrievalHit``. ``score`` mimics the incoming RRF score."""
    return RetrievalHit(
        book_id=uuid.UUID(int=book_idx),
        chunk_index=chunk_index,
        content_chunk=content or f"chunk-{book_idx}-{chunk_index}",
        metadata={"chunk_index": chunk_index},
        score=score,
        dense_score=0.5,
        sparse_score=0.1,
    )


def test_rerank_empty_hits_returns_empty_without_remote_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty input must not reach the remote scorer.

    An empty-library /search should never pay a network round-trip (or
    require a key). We monkeypatch the seam to raise so a regression
    here surfaces loudly — the same guarantee the in-process era pinned
    against the model loader.
    """

    def _explode(_query: str, _documents: list[str]) -> list[float]:
        msg = "remote scorer should not be called for empty input"
        raise AssertionError(msg)

    monkeypatch.setattr(rerank_module, "_score_pairs", _explode)
    out = rerank_module.rerank(query="x", hits=[], top_n=10)
    assert out == []


def test_rerank_reorders_by_relevance_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """Highest reranker score floats to position 0 regardless of input RRF order."""
    hits = [
        _hit(1, 0, score=0.99),  # high RRF, but the reranker scores it low (0.1)
        _hit(2, 0, score=0.20),  # low RRF, but the reranker scores it high (5.0)
        _hit(3, 0, score=0.50),  # middle (2.5)
    ]
    fake = _FakeScorer(scores=[0.1, 5.0, 2.5])
    monkeypatch.setattr(rerank_module, "_score_pairs", fake)

    out = rerank_module.rerank(query="q", hits=hits, top_n=3)
    assert [h.book_id.int for h in out] == [2, 3, 1]
    assert out[0].score == pytest.approx(5.0)
    assert out[1].score == pytest.approx(2.5)
    assert out[2].score == pytest.approx(0.1)


def test_rerank_truncates_to_top_n(monkeypatch: pytest.MonkeyPatch) -> None:
    """``top_n`` caps the returned list; lower-scoring hits are dropped."""
    hits = [_hit(i, 0, score=1.0 / (i + 1)) for i in range(5)]
    fake = _FakeScorer(scores=[1.0, 4.0, 2.0, 3.0, 5.0])
    monkeypatch.setattr(rerank_module, "_score_pairs", fake)

    out = rerank_module.rerank(query="q", hits=hits, top_n=2)
    # Top-2 by reranker score: 5.0 (idx 4) then 4.0 (idx 1).
    assert [h.book_id.int for h in out] == [4, 1]


def test_rerank_preserves_rrf_score_in_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """The RRF score the hit carried on entry survives as metadata['rrf_score']."""
    hits = [_hit(1, 0, score=0.0163), _hit(2, 0, score=0.0159)]
    fake = _FakeScorer(scores=[3.0, 7.0])
    monkeypatch.setattr(rerank_module, "_score_pairs", fake)

    out = rerank_module.rerank(query="q", hits=hits, top_n=2)
    # Reordered: book 2 (reranker 7.0) first, book 1 (reranker 3.0) second.
    assert out[0].book_id.int == 2
    assert out[0].score == pytest.approx(7.0)
    assert out[0].metadata["rrf_score"] == pytest.approx(0.0159)
    assert out[1].book_id.int == 1
    assert out[1].score == pytest.approx(3.0)
    assert out[1].metadata["rrf_score"] == pytest.approx(0.0163)


def test_rerank_preserves_per_arm_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    """``dense_score`` and ``sparse_score`` survive the reranking pass."""
    hits = [_hit(1, 0, score=0.1)]
    fake = _FakeScorer(scores=[4.2])
    monkeypatch.setattr(rerank_module, "_score_pairs", fake)

    out = rerank_module.rerank(query="q", hits=hits, top_n=1)
    assert out[0].dense_score == pytest.approx(0.5)
    assert out[0].sparse_score == pytest.approx(0.1)


def test_rerank_stable_tiebreak_on_equal_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    """Equal reranker scores preserve input (RRF) order — determinism gate."""
    hits = [_hit(1, 0, score=0.9), _hit(2, 0, score=0.7), _hit(3, 0, score=0.5)]
    fake = _FakeScorer(scores=[1.0, 1.0, 1.0])
    monkeypatch.setattr(rerank_module, "_score_pairs", fake)

    out = rerank_module.rerank(query="q", hits=hits, top_n=3)
    # All scores equal → input order preserved (index-based tiebreak in rerank()).
    assert [h.book_id.int for h in out] == [1, 2, 3]


def test_rerank_passes_query_and_documents_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scorer receives the query plus chunk texts in input order — wire contract.

    ``inference.rerank_scores`` fans the query out per document on the
    provider side; what this layer must guarantee is that the documents
    array matches the hits' order exactly (scores come back aligned).
    """
    hits = [
        _hit(1, 0, score=0.2, content="first passage"),
        _hit(2, 0, score=0.1, content="second passage"),
    ]
    fake = _FakeScorer(scores=[2.0, 1.0])
    monkeypatch.setattr(rerank_module, "_score_pairs", fake)

    rerank_module.rerank(query="my question", hits=hits, top_n=2)
    assert fake.calls == [("my question", ["first passage", "second passage"])]
