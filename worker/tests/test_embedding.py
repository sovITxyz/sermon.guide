"""Smoke tests for the BGE-Large embedder.

Two layers:

1. **Pure unit** — empty input short-circuits without touching the model.
   Runs everywhere, including CI without the HF cache.
2. **End-to-end** — load the real BGE-Large model and embed a few strings.
   Asserts shape, dtype, and that vectors come out L2-normalized (which is
   the precondition for Milvus' `COSINE` metric to behave as
   inner-product — ARCHITECTURE.md §3). Skipped without `HF_HOME` cache,
   matching `test_chunking.py`.
"""

# sentence-transformers types `encode()` widely; embedding.py already pyright-suppresses.
# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from embedding import EMBED_DIM, MODEL_NAME, embed


def _model_available() -> bool:
    if os.environ.get("HF_HUB_OFFLINE") == "1":
        return False
    cache = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    slug = MODEL_NAME.replace("/", "--")
    return (cache / "hub" / f"models--{slug}").is_dir()


def test_embed_empty_returns_zero_rows_no_model_load() -> None:
    """Empty input must not trigger a model download.

    Guards CI from accidentally pulling 1.3GB when nothing needs embedding.
    """
    out = embed([])
    assert out.shape == (0, EMBED_DIM)
    assert out.dtype == np.float32


@pytest.mark.skipif(
    not _model_available(),
    reason="BGE-Large model not in HF cache — set HF_HOME or prewarm to run",
)
def test_embed_real_texts_has_expected_shape_and_dtype() -> None:
    texts = [
        "Grace and peace to you from God our Father.",
        "Justification by faith alone.",
        "And the Word became flesh and dwelt among us.",
    ]
    out = embed(texts)
    assert out.shape == (len(texts), EMBED_DIM)
    assert out.dtype == np.float32


@pytest.mark.skipif(
    not _model_available(),
    reason="BGE-Large model not in HF cache — set HF_HOME or prewarm to run",
)
def test_embed_outputs_are_l2_normalized() -> None:
    """Each row must have ||v|| ≈ 1 so Milvus COSINE ≡ inner product.

    If this regresses, retrieval scores stop being interpretable as cosine
    similarity and the rank order shifts in ways the cross-encoder
    rerank (Phase 13) was not tuned for.
    """
    out = embed(["alpha", "beta", "gamma"])
    norms = np.linalg.norm(out, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5), f"norms={norms!r}"


@pytest.mark.skipif(
    not _model_available(),
    reason="BGE-Large model not in HF cache — set HF_HOME or prewarm to run",
)
def test_embed_is_deterministic_for_identical_input() -> None:
    """Same input → same vector. Lets dedup / golden tests rely on stable embeddings."""
    text = "The grass withers and the flowers fall, but the word of our God endures."
    a = embed([text])
    b = embed([text])
    assert np.array_equal(a, b)
