"""Sentence-level context pruning via BGE-M3 dense scoring. Remote since Phase 16b.

Phase 13 (ARCHITECTURE.md §2 "Context pruning", §5 lifecycle). After
the rerank in ``api/rerank.py`` selects the top-N most relevant chunks,
we still want to feed downstream stages (Phase 14 LLM summarization)
only the *parts* of each chunk that actually answer the query.
``highlight`` splits each chunk into sentences, scores every sentence
against the query with BGE-M3 dense embeddings (cosine, unit-
normalized), and drops sentences below threshold 0.5.

Target reduction per ARCHITECTURE.md §2 row "Context pruning": 70–80%
fewer input tokens to the LLM, without losing the answer-bearing
sentences.

## Model

``BAAI/bge-m3``'s dense head — sentence-vs-query cosine. Phase 16b
(ADR 0006): the embeddings come from the remote OpenAI-compatible
endpoint via ``worker/inference.py`` — the EXACT same weights the
in-process loader served, so threshold 0.5's calibration (and with it
the no-context → no-LLM-call anti-confabulation contract the Phase
14/16 live verifies pinned) carries over unchanged. The query and every
sentence ride ONE batched embeddings call per request.

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
linguistic intuition.

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
can see how aggressively each chunk was pruned without re-scoring.

## Failure mode

Same as ``api/rerank.py`` — a remote failure raises
``RemoteInferenceError``; since Phase 22 the retrieval caller
(``search.run_search``) catches it and returns the hits unpruned with
``"highlight"`` in the response's ``degraded`` list (logged loudly with
the traceback). Callers outside that path get the Phase 16b mapping in
``api/main.py`` (502; unset key → 503). The empty-input path is cheap,
key-free, and makes no remote call.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace

import numpy as np
from inference import embed_texts
from retrieval import RetrievalHit

MODEL_NAME = "BAAI/bge-m3"
HIGHLIGHT_THRESHOLD = 0.5

# Sentence boundary: sentence-ending punctuation followed by whitespace
# and the next sentence's likely start character (capital letter or an
# opening quote). See module docstring for the regex's known failure
# modes — none are content-lossy, only grouping-quirky.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'“‘])")


def _embed_batch(texts: list[str]) -> np.ndarray:
    """Embed *texts* with BGE-M3 dense remotely; the unit-test seam.

    Returns unit-normalized ``(len(texts), dim)`` float32 rows (the
    transport normalizes client-side), so cosine reduces to inner
    product downstream. Tests monkeypatch this — the same role the
    ``_model`` loader seam played in the in-process era.
    """
    return embed_texts(texts, model=MODEL_NAME)


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
    can attribute token reduction without re-scoring.
    """
    if not hits:
        return []

    per_hit_sentences = [_split_sentences(h.content_chunk) for h in hits]
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

    # ONE batched remote call per request: the query rides as row 0 and
    # every sentence follows, so the per-query cost is a single network
    # round-trip however many chunks survived the rerank.
    vectors = _embed_batch([query, *all_sentences])
    query_vec = np.asarray(vectors[0], dtype=np.float32)
    sentence_vecs = np.asarray(vectors[1:], dtype=np.float32)
    # All rows are unit-normalized by the transport, so cosine reduces to
    # inner product. Shape: (N_sentences, dim) @ (dim,) → (N_sentences,).
    scores = np.asarray(sentence_vecs @ query_vec, dtype=np.float32)

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
