"""Cross-encoder reranker — top-K from the hybrid arm → top-N reranked.

Phase 13 (ARCHITECTURE.md §2 "Reranking", §5 lifecycle). The hybrid
arm (Phase 12) returns up to 30 fused hits ordered by RRF score;
cross-encoder reranking is a precision pass that reorders the fan-out
by a single (query, chunk) relevance score per pair. The intent is to
surface the *best* matches over the recall-shaped fused list and to
demote false positives that a bi-encoder + RRF can't catch — concretely,
the dense-side false-positives at 0.5–0.6 COSINE that surface for
queries with no real corpus match (the Phase 12 audit's "Theodore
Roosevelt" failure mode).

## Why cross-encoder

Bi-encoder retrieval (BGE-Large + Postgres BM25) computes the query
and the chunk independently — the model never sees them together until
cosine or RRF combines the two ranks. A cross-encoder feeds the pair
through a single transformer with cross-attention so it can spot
fine-grained relevance signals (negation, scope, intent) that a dot
product can't. The tradeoff is that we pay one forward pass per pair,
not one shared encode; at 30 pairs per /search and ~ms each on
``ms-marco-MiniLM-L-6-v2``, this is fine.

## Model

``cross-encoder/ms-marco-MiniLM-L-6-v2`` is the standard pick: ~90MB,
6-layer MiniLM trained on the MS-MARCO passage-ranking dataset. The
swap candidate is ``cross-encoder/ms-marco-MiniLM-L-12-v2`` (slightly
better recall, ~2x slower); we'd switch by editing ARCHITECTURE.md §2
and the ``MODEL_NAME`` constant below.

## Process-level singleton

The ``CrossEncoder`` is loaded lazily once per process via
``@lru_cache``. First call after process boot pays the model load;
every subsequent call is one forward pass per pair. Mirrors the loader
pattern in ``worker/embedding.py``.

## Output semantics

``rerank`` returns the top-N hits with their ``score`` field replaced
by the cross-encoder relevance score (higher = better; unbounded —
typical range roughly ``[-15, +15]`` on this model). The previous
``score`` (RRF, from Phase 12) is preserved on the returned hit's
``metadata["rrf_score"]`` for debug tooling that wants to compare what
the reranker promoted vs. what RRF surfaced. Per-arm ``dense_score``
and ``sparse_score`` survive unchanged so the full provenance is still
visible from a single returned hit.

## Failure mode

A model load failure (HF cache cold + no network) or an OOM raises
through to the FastAPI handler, which 500s — same fail-loud posture as
the dense + sparse arms in Phase 12 (audit findings, ``docs/PHASES.md``
row 12). Graceful degradation (fall back to raw RRF top-K when rerank
fails) would need an explicit ``return_exceptions`` policy and would
mask model issues from operators; defer until v0 traffic data motivates
it.
"""

# sentence-transformers ships type info but the CrossEncoder.predict
# return type is `np.ndarray | Tensor | list[Tensor]` depending on flags;
# we always request the numpy path so the runtime is ndarray. Same
# relaxation pattern worker/embedding.py uses on the SentenceTransformer
# encode path.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from functools import lru_cache

import numpy as np
from retrieval import RetrievalHit
from sentence_transformers import CrossEncoder

MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"
RERANK_FANOUT = 30  # how many hybrid hits to feed the reranker
_DEVICE = "cpu"


@lru_cache(maxsize=1)
def _model() -> CrossEncoder:
    """Lazily load the cross-encoder once per process. First call may download ~90MB."""
    return CrossEncoder(MODEL_NAME, device=_DEVICE)


def rerank(
    *,
    query: str,
    hits: Sequence[RetrievalHit],
    top_n: int,
) -> list[RetrievalHit]:
    """Score (query, chunk) pairs with the cross-encoder; return top-N reranked.

    Returns up to *top_n* hits sorted by cross-encoder relevance score
    descending. Each returned hit has:

    - ``score`` — the cross-encoder relevance score (higher = better,
      unbounded; this replaces the RRF score the input hit carried).
    - ``metadata["rrf_score"]`` — the RRF score the hit had on entry,
      preserved for debug tooling.
    - ``dense_score`` / ``sparse_score`` — unchanged from the hybrid arm.

    Empty input short-circuits to ``[]`` — the model is *not* loaded
    when there's nothing to rerank, which keeps the empty-library /
    nothing-matched paths cheap.
    """
    if not hits:
        return []
    pairs = [(query, h.content_chunk) for h in hits]
    raw = _model().predict(pairs, show_progress_bar=False, convert_to_numpy=True)
    scores = np.asarray(raw, dtype=np.float32)
    # Pair each (score, hit) and sort by score desc. Use the index as a
    # tiebreaker so equal scores preserve input (RRF) order — important
    # for the unit tests that pin determinism on synthetic equal scores.
    ranked = sorted(
        enumerate(zip(scores.tolist(), hits, strict=True)),
        key=lambda item: (-item[1][0], item[0]),
    )
    out: list[RetrievalHit] = []
    for _idx, (score, hit) in ranked[:top_n]:
        new_metadata = {**hit.metadata, "rrf_score": hit.score}
        out.append(replace(hit, score=float(score), metadata=new_metadata))
    return out
