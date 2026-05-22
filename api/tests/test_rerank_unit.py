"""Unit tests for the cross-encoder reranker glue (Phase 13).

Live cross-encoder inference is covered by the retrieval goldens (and
manual e2e against the 5-book corpus); this file pins the deterministic
glue around the model:

- Empty input short-circuits without loading the model.
- Top-N truncation respects the requested cap.
- The score replacement preserves RRF as ``metadata["rrf_score"]``.
- Per-arm ``dense_score`` / ``sparse_score`` survive reranking.
- Stable ordering on tied scores (RRF/input order is the tiebreak).

We monkeypatch ``rerank._model`` so no HF cache, no torch, no network.
The real cross-encoder is exercised by the golden retrieval suite in
``worker/tests/test_retrieval_golden.py``.
"""

# Tests reach into module internals on purpose. pytest.approx ships
# loose stubs that pyright strict reports as Unknown — silence per-file.
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import uuid
from typing import Any

import numpy as np
import pytest
from retrieval import RetrievalHit

import rerank as rerank_module


class _FakeCrossEncoder:
    """In-process stand-in for ``sentence_transformers.CrossEncoder``.

    ``predict`` returns a pre-baked score array keyed off the pair list
    so each test can drive any ordering it wants without touching the
    real model. ``_calls`` records every call for assertions about
    load-elision on empty inputs.
    """

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores
        self._calls: list[list[tuple[str, str]]] = []

    def predict(
        self,
        sentences: list[tuple[str, str]],
        **_: Any,  # noqa: ANN401 — match CrossEncoder.predict signature loosely
    ) -> np.ndarray:
        self._calls.append(list(sentences))
        return np.asarray(self._scores[: len(sentences)], dtype=np.float32)


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


def test_rerank_empty_hits_returns_empty_without_model_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty input must not load the cross-encoder.

    The architecture loads the cross-encoder lazily; an empty-library
    /search should never pay the ~90MB model load. We monkeypatch the
    loader to raise so a regression here surfaces loudly.
    """

    def _explode() -> None:
        msg = "model should not be loaded for empty input"
        raise AssertionError(msg)

    monkeypatch.setattr(rerank_module, "_model", _explode)
    out = rerank_module.rerank(query="x", hits=[], top_n=10)
    assert out == []


def test_rerank_reorders_by_cross_encoder_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """Highest cross-encoder score floats to position 0 regardless of input RRF order."""
    hits = [
        _hit(1, 0, score=0.99),  # high RRF, but the encoder scores it low (0.1)
        _hit(2, 0, score=0.20),  # low RRF, but the encoder scores it high (5.0)
        _hit(3, 0, score=0.50),  # middle (2.5)
    ]
    fake = _FakeCrossEncoder(scores=[0.1, 5.0, 2.5])
    monkeypatch.setattr(rerank_module, "_model", lambda: fake)

    out = rerank_module.rerank(query="q", hits=hits, top_n=3)
    assert [h.book_id.int for h in out] == [2, 3, 1]
    assert out[0].score == pytest.approx(5.0)
    assert out[1].score == pytest.approx(2.5)
    assert out[2].score == pytest.approx(0.1)


def test_rerank_truncates_to_top_n(monkeypatch: pytest.MonkeyPatch) -> None:
    """``top_n`` caps the returned list; lower-scoring hits are dropped."""
    hits = [_hit(i, 0, score=1.0 / (i + 1)) for i in range(5)]
    fake = _FakeCrossEncoder(scores=[1.0, 4.0, 2.0, 3.0, 5.0])
    monkeypatch.setattr(rerank_module, "_model", lambda: fake)

    out = rerank_module.rerank(query="q", hits=hits, top_n=2)
    # Top-2 by encoder score: 5.0 (idx 4) then 4.0 (idx 1).
    assert [h.book_id.int for h in out] == [4, 1]


def test_rerank_preserves_rrf_score_in_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """The RRF score the hit carried on entry survives as metadata['rrf_score']."""
    hits = [_hit(1, 0, score=0.0163), _hit(2, 0, score=0.0159)]
    fake = _FakeCrossEncoder(scores=[3.0, 7.0])
    monkeypatch.setattr(rerank_module, "_model", lambda: fake)

    out = rerank_module.rerank(query="q", hits=hits, top_n=2)
    # Reordered: book 2 (encoder 7.0) first, book 1 (encoder 3.0) second.
    assert out[0].book_id.int == 2
    assert out[0].score == pytest.approx(7.0)
    assert out[0].metadata["rrf_score"] == pytest.approx(0.0159)
    assert out[1].book_id.int == 1
    assert out[1].score == pytest.approx(3.0)
    assert out[1].metadata["rrf_score"] == pytest.approx(0.0163)


def test_rerank_preserves_per_arm_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    """``dense_score`` and ``sparse_score`` survive the reranking pass."""
    hits = [_hit(1, 0, score=0.1)]
    fake = _FakeCrossEncoder(scores=[4.2])
    monkeypatch.setattr(rerank_module, "_model", lambda: fake)

    out = rerank_module.rerank(query="q", hits=hits, top_n=1)
    assert out[0].dense_score == pytest.approx(0.5)
    assert out[0].sparse_score == pytest.approx(0.1)


def test_rerank_stable_tiebreak_on_equal_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    """Equal cross-encoder scores preserve input (RRF) order — determinism gate."""
    hits = [_hit(1, 0, score=0.9), _hit(2, 0, score=0.7), _hit(3, 0, score=0.5)]
    fake = _FakeCrossEncoder(scores=[1.0, 1.0, 1.0])
    monkeypatch.setattr(rerank_module, "_model", lambda: fake)

    out = rerank_module.rerank(query="q", hits=hits, top_n=3)
    # All scores equal → input order preserved (index-based tiebreak in rerank()).
    assert [h.book_id.int for h in out] == [1, 2, 3]


def test_rerank_passes_query_as_first_pair_element(monkeypatch: pytest.MonkeyPatch) -> None:
    """The encoder receives (query, chunk) pairs in that order — model contract."""
    hits = [_hit(1, 0, score=0.1, content="some passage")]
    fake = _FakeCrossEncoder(scores=[2.0])
    monkeypatch.setattr(rerank_module, "_model", lambda: fake)

    rerank_module.rerank(query="my question", hits=hits, top_n=1)
    assert fake._calls == [[("my question", "some passage")]]
