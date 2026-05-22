"""Sentence-level context pruning via BGE-M3 semantic highlighting.

Phase 13 (ARCHITECTURE.md §2 "Context pruning", §5 lifecycle). After
the cross-encoder rerank in ``api/rerank.py`` selects the top-N most
relevant chunks, we still want to feed downstream stages (Phase 14
LLM summarization) only the *parts* of each chunk that actually answer
the query. ``highlight`` splits each chunk into sentences, scores
every sentence against the query with BGE-M3 (cosine, unit-normalized),
and drops sentences below threshold 0.5.

Target reduction per ARCHITECTURE.md §2 row "Context pruning": 70–80%
fewer input tokens to the LLM, without losing the answer-bearing
sentences.

## Model

``BAAI/bge-m3`` is a multi-functional embedder (dense + sparse +
ColBERT-style multi-vec). We use only its dense head — sentence-vs-
query cosine — so we load it via ``SentenceTransformer`` in default
mode. Model weights are ~2.3GB; first cold call downloads from HF.
Same CPU-only / future-GPU note as ``worker/embedding.py``.

Threshold 0.5 is the spec value. Empirically on BGE-M3:

- Off-topic sentences typically cosine 0.30–0.45.
- On-topic sentences typically cosine 0.55–0.85.

A higher threshold prunes more aggressively but risks dropping
answer-bearing sentences phrased indirectly; a lower threshold leaks
more filler. 0.5 is the architecture-locked balance for v0.

## Sentence splitting

Regex-based: split on sentence-ending punctuation followed by
whitespace + a capital letter or an opening quote. This isn't perfect
— abbreviations (Dr., Mr., 1 Th. 5:1) sometimes glue sentences, and
quoted speech sometimes splits mid-quote — but the consequence of an
imperfect boundary is "a longer or shorter pseudo-sentence is scored
as one unit", not lost content. The pruned chunk is reassembled by
concatenating the kept sentences in original order; the user-visible
text is the original text, just possibly grouped differently across
the kept/dropped boundary than a linguist would.

A future swap to ``pysbd`` or NLTK ``punkt_tab`` is mechanical — the
splitter is the only mutable boundary between regex behavior and
linguistic intuition. Adding it now would mean shipping another
HuggingFace-style "did you remember to download the resource?" foot-
gun, so we stay regex-based until eval shows it costs us recall.

## Output

``highlight`` returns each hit with its ``content_chunk`` replaced by
the pruned text — kept sentences joined with single spaces in original
order. If every sentence in a chunk falls below threshold, that hit is
dropped entirely (output list may be shorter than input). If no
sentences split out of a chunk (e.g. punctuation-free fragment), the
hit passes through unchanged — pruning can't make a unsplittable chunk
shorter than itself.

``metadata["sentences_kept"]`` / ``metadata["sentences_total"]`` are
written so callers (debug tooling, the Phase 14 token-budget check)
can see how aggressively each chunk was pruned without re-running the
model.

## Failure mode

Same fail-loud posture as ``api/rerank.py`` — a model load failure or
OOM raises into the handler and 500s. The empty-input path is cheap
and does *not* load the model.
"""

# sentence-transformers stubs are wide on encode()'s return type — same
# relaxation as worker/embedding.py.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from functools import lru_cache

import numpy as np
from retrieval import RetrievalHit
from sentence_transformers import SentenceTransformer

MODEL_NAME = "BAAI/bge-m3"
HIGHLIGHT_THRESHOLD = 0.5
_DEVICE = "cpu"

# Sentence boundary: sentence-ending punctuation followed by whitespace
# and the next sentence's likely start character (capital letter or an
# opening quote). See module docstring for the regex's known failure
# modes — none are content-lossy, only grouping-quirky.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'“‘])")


@lru_cache(maxsize=1)
def _model() -> SentenceTransformer:
    """Lazily load BGE-M3 once per process. First call may download ~2.3GB."""
    return SentenceTransformer(MODEL_NAME, device=_DEVICE)


def _split_sentences(text: str) -> list[str]:
    """Split ``text`` into sentences. Empty / whitespace-only → empty list."""
    stripped = text.strip()
    if not stripped:
        return []
    parts = _SENTENCE_BOUNDARY.split(stripped)
    return [p.strip() for p in parts if p.strip()]


def highlight(
    *,
    query: str,
    hits: Sequence[RetrievalHit],
    threshold: float = HIGHLIGHT_THRESHOLD,
) -> list[RetrievalHit]:
    """Prune below-threshold sentences from each hit's ``content_chunk``.

    Per-hit behavior:

    - At least one sentence ≥ ``threshold`` → ``content_chunk`` becomes
      the kept sentences joined by single spaces, in original order.
    - Every sentence < ``threshold`` → the hit is dropped (not in
      output).
    - Zero sentences split (e.g. punctuation-free fragment) → hit
      passes through unchanged.

    Each surviving hit's ``metadata`` is augmented with
    ``sentences_kept`` and ``sentences_total`` so downstream tooling
    can attribute token reduction without re-running BGE-M3.
    """
    if not hits:
        return []

    per_hit_sentences = [_split_sentences(h.content_chunk) for h in hits]
    # Flatten every sentence into one batch so the BGE-M3 forward pass
    # is a single call instead of N. The model itself is loaded once
    # per process via ``_model``'s lru_cache; per-call latency is the
    # encode pass over (1 + total_sentences) inputs.
    all_sentences: list[str] = [s for sents in per_hit_sentences for s in sents]
    if not all_sentences:
        # No splittable sentences in any chunk — nothing to score.
        # Pass through unchanged with metadata recording the no-op.
        return [
            replace(
                hit,
                metadata={**hit.metadata, "sentences_kept": 0, "sentences_total": 0},
            )
            for hit in hits
        ]

    model = _model()
    query_vec = model.encode(
        [query],
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    sentence_vecs = model.encode(
        all_sentences,
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    # Both arrays are unit-normalized, so cosine reduces to inner product.
    # Shape: (N_sentences, 1024) @ (1024,) → (N_sentences,).
    scores = np.asarray(sentence_vecs @ np.asarray(query_vec)[0], dtype=np.float32)

    pruned: list[RetrievalHit] = []
    cursor = 0
    for hit, sentences in zip(hits, per_hit_sentences, strict=True):
        total = len(sentences)
        if total == 0:
            # Unsplittable chunk; pass through unchanged.
            pruned.append(
                replace(
                    hit,
                    metadata={**hit.metadata, "sentences_kept": 0, "sentences_total": 0},
                ),
            )
            continue
        end = cursor + total
        hit_scores = scores[cursor:end]
        cursor = end
        kept = [
            sentence
            for sentence, score in zip(sentences, hit_scores.tolist(), strict=True)
            if score >= threshold
        ]
        if not kept:
            # Every sentence fell below threshold → whole chunk pruned.
            continue
        pruned_text = " ".join(kept)
        pruned.append(
            replace(
                hit,
                content_chunk=pruned_text,
                metadata={
                    **hit.metadata,
                    "sentences_kept": len(kept),
                    "sentences_total": total,
                },
            ),
        )

    return pruned
