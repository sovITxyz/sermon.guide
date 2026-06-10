"""Reranker — top-K from the hybrid arm → top-N reranked. Remote since Phase 16b.

Phase 13 (ARCHITECTURE.md §2 "Reranking", §5 lifecycle). The hybrid
arm (Phase 12) returns up to 30 fused hits ordered by RRF score;
reranking is a precision pass that reorders the fan-out by a single
(query, chunk) relevance score per pair. The intent is to surface the
*best* matches over the recall-shaped fused list and to demote false
positives that a bi-encoder + RRF can't catch — concretely, the
dense-side false-positives at 0.5–0.6 COSINE that surface for queries
with no real corpus match (the Phase 12 audit's "Theodore Roosevelt"
failure mode).

## Why a cross-attention reranker

Bi-encoder retrieval (BGE-Large + Postgres BM25) computes the query
and the chunk independently — the model never sees them together until
cosine or RRF combines the two ranks. A reranker feeds the pair through
a single transformer with cross-attention so it can spot fine-grained
relevance signals (negation, scope, intent) that a dot product can't.

## Model

Phase 16b (ADR 0006): the in-process ``cross-encoder/ms-marco-MiniLM-
L-6-v2`` became a remote call to ``Qwen/Qwen3-Reranker-8B`` via
``worker/inference.py:rerank_scores`` — a large quality jump over the
2021 MiniLM (operator picked the 8B over the cheaper 0.6B/4B siblings
for maximum accuracy; ~$0.0005 per 30-doc query). The model id is
env-driven (``SERMON_RERANK_MODEL``), so dropping to a smaller sibling
is an env flip, not a code change.

## Output semantics

``rerank`` returns the top-N hits with their ``score`` field replaced
by the reranker relevance score (higher = better; the Qwen3 rerankers
return relevance scores in roughly ``[0, 1]`` — only the *ordering* is
load-bearing downstream, no absolute-score threshold consumes this
value). The previous ``score`` (RRF, from Phase 12) is preserved on the
returned hit's ``metadata["rrf_score"]`` for debug tooling that wants
to compare what the reranker promoted vs. what RRF surfaced. Per-arm
``dense_score`` and ``sparse_score`` survive unchanged so the full
provenance is still visible from a single returned hit.

## Failure mode

A remote failure raises ``RemoteInferenceError`` through to the FastAPI
layer, which maps it to a 502 naming the provider (``api/main.py``;
the Phase 14b pattern) — an unset ``DEEPINFRA_API_KEY`` maps to a 503.
Graceful degradation (fall back to raw RRF top-K when rerank fails)
would mask provider issues from operators; defer until traffic data
motivates it (same posture Phase 13 took for model-load failures).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from inference import rerank_scores
from retrieval import RetrievalHit

RERANK_FANOUT = 30  # how many hybrid hits to feed the reranker


def _score_pairs(query: str, documents: list[str]) -> list[float]:
    """Score (query, document) pairs remotely; the unit-test seam.

    Thin indirection over ``inference.rerank_scores`` so tests can
    monkeypatch the scoring without a network (the same role the
    ``_model`` loader seam played in the in-process era).
    """
    return rerank_scores(query=query, documents=documents)


def rerank(
    *,
    query: str,
    hits: Sequence[RetrievalHit],
    top_n: int,
) -> list[RetrievalHit]:
    """Score (query, chunk) pairs with the remote reranker; return top-N.

    Returns up to *top_n* hits sorted by reranker relevance score
    descending. Each returned hit has:

    - ``score`` — the reranker relevance score (higher = better; this
      replaces the RRF score the input hit carried).
    - ``metadata["rrf_score"]`` — the RRF score the hit had on entry,
      preserved for debug tooling.
    - ``dense_score`` / ``sparse_score`` — unchanged from the hybrid arm.

    Empty input short-circuits to ``[]`` — no remote call is made when
    there's nothing to rerank, which keeps the empty-library /
    nothing-matched paths cheap (and key-free).
    """
    if not hits:
        return []
    scores = _score_pairs(query, [h.content_chunk for h in hits])
    # Pair each (score, hit) and sort by score desc. Use the index as a
    # tiebreaker so equal scores preserve input (RRF) order — important
    # for the unit tests that pin determinism on synthetic equal scores.
    ranked = sorted(
        enumerate(zip(scores, hits, strict=True)),
        key=lambda item: (-item[1][0], item[0]),
    )
    out: list[RetrievalHit] = []
    for _idx, (score, hit) in ranked[:top_n]:
        new_metadata = {**hit.metadata, "rrf_score": hit.score}
        out.append(replace(hit, score=float(score), metadata=new_metadata))
    return out
