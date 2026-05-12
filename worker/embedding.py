"""BGE-Large embeddings for chunked text → Milvus.

`embed(texts)` returns a `(N, 1024)` float32 array of L2-normalized
embeddings produced by `BAAI/bge-large-en-v1.5`. Milvus stores them with
metric `COSINE`, which on unit vectors is equivalent to inner-product —
see ARCHITECTURE.md §3.

The model loads once per process via `@lru_cache`; the first call after a
cold venv triggers a ~1.3GB HuggingFace download and CPU inference. GPU
inference is deferred (Phase 6 spec); swap `device="cpu"` for `"cuda"`
once a GPU runtime exists.

Chunking (Phase 5) already loads the same model via
`llama-index-embeddings-huggingface` for boundary detection. The model
*file* is shared (one HF Hub cache entry); each loader keeps its own
in-memory copy. Consolidating to a single loader is a future micro-opt,
not Phase 6 scope.

CLI (run from `worker/`):

    uv run python -c 'from embedding import embed; print(embed(["hello"]).shape)'
"""

# sentence-transformers types `encode()` as a union covering Tensor / ndarray
# / list-of-tensors depending on flags — we always request `convert_to_numpy=True`
# so the runtime is ndarray. Cast to silence the union-narrowing complaint
# rather than trust pyright on a third-party stub that is conservatively wide.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

from functools import lru_cache

import numpy as np
from sentence_transformers import SentenceTransformer

MODEL_NAME = "BAAI/bge-large-en-v1.5"
EMBED_DIM = 1024
_DEVICE = "cpu"


@lru_cache(maxsize=1)
def _model() -> SentenceTransformer:
    """Lazily load BGE-Large once per process. First call may download ~1.3GB."""
    return SentenceTransformer(MODEL_NAME, device=_DEVICE)


def embed(texts: list[str]) -> np.ndarray:
    """Embed *texts* into a `(len(texts), 1024)` float32 array.

    Output is L2-normalized so Milvus' `COSINE` metric reduces to inner
    product. Returns a zero-row array when *texts* is empty so callers can
    pass through `np.ndarray` without a None branch.
    """
    if not texts:
        return np.zeros((0, EMBED_DIM), dtype=np.float32)

    raw = _model().encode(
        texts,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    arr = raw.astype(np.float32, copy=False)
    if arr.shape != (len(texts), EMBED_DIM):
        msg = (
            f"BGE-Large returned shape {arr.shape}; expected "
            f"({len(texts)}, {EMBED_DIM}). Model swap or version drift?"
        )
        raise RuntimeError(msg)
    return arr
