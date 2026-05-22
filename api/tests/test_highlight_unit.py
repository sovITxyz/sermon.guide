"""Unit tests for BGE-M3 sentence-level highlighting glue (Phase 13).

Live BGE-M3 inference is exercised by the goldens + manual e2e; this
file pins the deterministic glue:

- Empty input short-circuits without loading the model.
- Sentence splitter handles common boundaries + abbreviation edge cases.
- Below-threshold sentences are dropped; above-threshold survive.
- Whole-chunk-pruned hits are removed from output.
- Unsplittable chunks pass through unchanged.
- ``metadata["sentences_kept" / "sentences_total"]`` records the prune ratio.

We monkeypatch ``highlight._model`` so no HF cache, no torch, no
network. The real BGE-M3 is exercised by the manual e2e against the
5-book corpus (see Phase 13 verify in docs/PHASES.md).
"""

# Tests reach into module internals on purpose. pytest.approx + numpy
# stubs are wide — silence per-file rather than fight them.
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import uuid
from typing import Any

import numpy as np
import pytest
from retrieval import RetrievalHit

import highlight as highlight_module


class _FakeBGEM3:
    """In-process stand-in for ``SentenceTransformer`` returning unit vectors.

    ``encode`` returns an array whose i-th row is constructed so that
    its inner product with a chosen ``query_vec`` equals a chosen
    per-sentence score. The test driver builds the score plan; the
    fake reverse-engineers the right vectors.
    """

    def __init__(self, query_score_plan: dict[str, float]) -> None:
        """``query_score_plan`` maps sentence text → desired cosine-vs-query score."""
        self._plan = query_score_plan

    def encode(
        self,
        texts: list[str],
        **_: Any,  # noqa: ANN401 — match SentenceTransformer.encode loosely
    ) -> np.ndarray:
        # Two-dim space is enough: the query is (1, 0); each sentence is
        # (score, sqrt(1-score^2)) so query·sentence = score. Off-plan
        # sentences default to 0.0.
        out = np.zeros((len(texts), 2), dtype=np.float32)
        for i, t in enumerate(texts):
            if t == _QUERY:
                out[i] = np.asarray([1.0, 0.0], dtype=np.float32)
            else:
                score = self._plan.get(t, 0.0)
                # Clamp to [-1, 1] so sqrt argument stays non-negative.
                score = max(-1.0, min(1.0, score))
                out[i] = np.asarray([score, (1.0 - score * score) ** 0.5], dtype=np.float32)
        return out


_QUERY = "what does this say about grace"


def _hit(book_idx: int, chunk_index: int, content: str, *, score: float = 0.1) -> RetrievalHit:
    return RetrievalHit(
        book_id=uuid.UUID(int=book_idx),
        chunk_index=chunk_index,
        content_chunk=content,
        metadata={"chunk_index": chunk_index},
        score=score,
        dense_score=0.7,
    )


def test_highlight_empty_hits_returns_empty_without_model_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty input must not load BGE-M3.

    BGE-M3 is ~2.3GB; an empty-library /search should never pay the
    model load. Mirrors the same guarantee in test_rerank_unit.py.
    """

    def _explode() -> None:
        msg = "model should not be loaded for empty input"
        raise AssertionError(msg)

    monkeypatch.setattr(highlight_module, "_model", _explode)
    out = highlight_module.highlight(query=_QUERY, hits=[])
    assert out == []


def test_split_sentences_handles_basic_boundaries() -> None:
    """Period + capital, exclamation, question mark, single-sentence chunk."""
    text = "First sentence. Second sentence! Third sentence? Fourth one."
    assert highlight_module._split_sentences(text) == [
        "First sentence.",
        "Second sentence!",
        "Third sentence?",
        "Fourth one.",
    ]


def test_split_sentences_empty_and_whitespace_only() -> None:
    """Blank / whitespace input → empty list, no crash."""
    assert highlight_module._split_sentences("") == []
    assert highlight_module._split_sentences("   \n\t  ") == []


def test_split_sentences_handles_quoted_dialog() -> None:
    """Sentence-ending punctuation followed by an opening quote is a boundary."""
    text = 'He said one thing. "Then another," she replied.'
    out = highlight_module._split_sentences(text)
    assert out[0] == "He said one thing."
    assert "Then another" in out[1]


def test_split_sentences_unsplittable_chunk_returns_single_element() -> None:
    """A fragment with no sentence-ending punctuation comes back as one piece."""
    text = "this fragment has no period and is not capitalized after spaces"
    assert highlight_module._split_sentences(text) == [text]


def test_highlight_drops_below_threshold_sentences(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sentences scoring < 0.5 are pruned; the chunk text shrinks accordingly."""
    content = "Grace abounds throughout scripture. The weather report is sunny today."
    score_plan = {
        "Grace abounds throughout scripture.": 0.8,
        "The weather report is sunny today.": 0.2,
    }
    monkeypatch.setattr(highlight_module, "_model", lambda: _FakeBGEM3(score_plan))

    out = highlight_module.highlight(query=_QUERY, hits=[_hit(1, 0, content)])
    assert len(out) == 1
    assert out[0].content_chunk == "Grace abounds throughout scripture."
    assert out[0].metadata["sentences_kept"] == 1
    assert out[0].metadata["sentences_total"] == 2


def test_highlight_drops_entire_chunk_when_all_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chunk with no above-threshold sentence is dropped from output entirely."""
    content_a = "On-topic sentence about grace. Another on-topic point."
    content_b = "Completely unrelated narrative. Off-topic filler text."
    score_plan = {
        "On-topic sentence about grace.": 0.7,
        "Another on-topic point.": 0.6,
        "Completely unrelated narrative.": 0.2,
        "Off-topic filler text.": 0.1,
    }
    monkeypatch.setattr(highlight_module, "_model", lambda: _FakeBGEM3(score_plan))

    out = highlight_module.highlight(
        query=_QUERY,
        hits=[_hit(1, 0, content_a), _hit(2, 0, content_b)],
    )
    assert [h.book_id.int for h in out] == [1]  # book 2 was dropped entirely
    assert out[0].metadata["sentences_kept"] == 2
    assert out[0].metadata["sentences_total"] == 2


def test_highlight_preserves_sentence_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kept sentences are joined in original order, not score-sorted order."""
    content = "First on-topic. Off-topic middle. Second on-topic."
    score_plan = {
        "First on-topic.": 0.9,
        "Off-topic middle.": 0.1,
        "Second on-topic.": 0.6,
    }
    monkeypatch.setattr(highlight_module, "_model", lambda: _FakeBGEM3(score_plan))

    out = highlight_module.highlight(query=_QUERY, hits=[_hit(1, 0, content)])
    assert out[0].content_chunk == "First on-topic. Second on-topic."


def test_highlight_threshold_boundary_is_inclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sentence scoring exactly at threshold is kept (>= threshold)."""
    content = "Boundary sentence."
    score_plan = {"Boundary sentence.": 0.5}
    monkeypatch.setattr(highlight_module, "_model", lambda: _FakeBGEM3(score_plan))

    out = highlight_module.highlight(query=_QUERY, hits=[_hit(1, 0, content)], threshold=0.5)
    assert len(out) == 1
    assert out[0].content_chunk == "Boundary sentence."


def test_highlight_unsplittable_chunk_passes_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chunk that doesn't split into sentences (no terminal punctuation +
    capital follow-on) is preserved as-is. The chunk text isn't touched, and
    the metadata records ``sentences_total=1`` (it's one pseudo-sentence)."""
    content = "fragment without sentence boundaries that still got retrieved"
    score_plan = {content: 0.9}
    monkeypatch.setattr(highlight_module, "_model", lambda: _FakeBGEM3(score_plan))

    out = highlight_module.highlight(query=_QUERY, hits=[_hit(1, 0, content)])
    assert len(out) == 1
    assert out[0].content_chunk == content
    # _split_sentences returns 1 element (the whole fragment); BGE-M3
    # scores it 0.9, so it survives as one "kept" sentence.
    assert out[0].metadata["sentences_kept"] == 1
    assert out[0].metadata["sentences_total"] == 1


def test_highlight_records_prune_ratio_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every output hit carries sentences_kept / sentences_total counts."""
    content = "Keep one. Drop one. Drop two. Keep two."
    score_plan = {
        "Keep one.": 0.8,
        "Drop one.": 0.2,
        "Drop two.": 0.1,
        "Keep two.": 0.7,
    }
    monkeypatch.setattr(highlight_module, "_model", lambda: _FakeBGEM3(score_plan))

    out = highlight_module.highlight(query=_QUERY, hits=[_hit(1, 0, content)])
    assert len(out) == 1
    assert out[0].metadata["sentences_kept"] == 2
    assert out[0].metadata["sentences_total"] == 4
    assert out[0].content_chunk == "Keep one. Keep two."


def test_highlight_preserves_existing_metadata_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing metadata (chunk_index, filename, parent_section, rrf_score
    from a rerank pass) is preserved when highlight adds its own keys."""
    content = "Survives. Drops."
    score_plan = {"Survives.": 0.9, "Drops.": 0.1}
    monkeypatch.setattr(highlight_module, "_model", lambda: _FakeBGEM3(score_plan))

    hit = RetrievalHit(
        book_id=uuid.UUID(int=1),
        chunk_index=4,
        content_chunk=content,
        metadata={
            "chunk_index": 4,
            "filename": "sample.pdf",
            "parent_section": "Intro",
            "rrf_score": 0.0163,
        },
        score=4.2,  # post-rerank score
        dense_score=0.7,
        sparse_score=None,
    )

    out = highlight_module.highlight(query=_QUERY, hits=[hit])
    assert len(out) == 1
    assert out[0].metadata["chunk_index"] == 4
    assert out[0].metadata["filename"] == "sample.pdf"
    assert out[0].metadata["parent_section"] == "Intro"
    assert out[0].metadata["rrf_score"] == pytest.approx(0.0163)
    assert out[0].metadata["sentences_kept"] == 1
    assert out[0].metadata["sentences_total"] == 2
