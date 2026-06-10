"""BGE-Large embeddings for chunked text → Milvus — remote since Phase 16b.

`embed(texts)` returns a `(N, 1024)` float32 array of L2-normalized
embeddings produced by `BAAI/bge-large-en-v1.5`. Milvus stores them with
metric `COSINE`, which on unit vectors is equivalent to inner-product —
see ARCHITECTURE.md §3.

Phase 16b (ADR 0006): the model no longer loads in-process. The body is
a remote call through `inference.embed_texts` to an OpenAI-compatible
embeddings endpoint serving the EXACT same weights — which is what keeps
every existing Milvus vector valid and every calibrated score floor
meaningful. The signature, output shape, dtype, normalization, and the
empty-input short-circuit are unchanged from the in-process era.

## Embedding-space guard

A deployment's vectors live in exactly one model's embedding space.
Before the first real embed of a process, `_verify_embedding_space`
compares `SERMON_EMBEDDINGS_MODEL` against the `embedding_model_id` row
in Postgres `meta` (seeded by migration 0003) and raises if they
disagree — silent provider/model drift would mix embedding spaces and
quietly destroy retrieval, so it fails loudly instead. Changing
embedders is a deliberate migration (re-embed, recalibrate, update the
row), never an env flip.

CLI (run from `worker/`; requires DEEPINFRA_API_KEY):

    uv run python -c 'from embedding import embed; print(embed(["hello"]).shape)'
"""

from __future__ import annotations

import numpy as np
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db import Meta, get_sync_session_factory
from inference import embed_texts
from inference import settings as inference_settings

# The locked retrieval embedder (ARCHITECTURE.md §2, ADR 0003/0006). Reads
# the env-driven transport setting so the pin and the wire request can
# never disagree.
MODEL_NAME = inference_settings.embeddings_model
EMBED_DIM = 1024

_META_KEY = "embedding_model_id"

# Once-per-process latch for the guard below. Module-level (not lru_cache)
# so tests can reset it and monkeypatch `_verify_embedding_space` cleanly.
_space_verified = False


def _verify_embedding_space() -> None:
    """Refuse to embed when the configured model ≠ the deployment's recorded one.

    Reads the ``meta`` row once per process (the sync session mirrors the
    worker's runtime model; the api calls this off the event loop inside
    ``asyncio.to_thread`` along with the embed itself). A missing row —
    a pre-0003 database — is recorded as the currently configured model,
    matching the migration's seed semantics.
    """
    global _space_verified  # noqa: PLW0603 — once-per-process latch, see module docstring
    if _space_verified:
        return

    configured = inference_settings.embeddings_model
    sf = get_sync_session_factory()
    with sf() as session, session.begin():
        row = session.get(Meta, _META_KEY)
        if row is None:
            session.execute(
                pg_insert(Meta)
                .values(key=_META_KEY, value=configured)
                .on_conflict_do_nothing(index_elements=[Meta.key]),
            )
            recorded = configured
        else:
            recorded = row.value

    if recorded != configured:
        msg = (
            f"Embedding-space mismatch: SERMON_EMBEDDINGS_MODEL={configured!r} but this "
            f"deployment's vectors were embedded with {recorded!r} (meta.{_META_KEY}). "
            f"Refusing to mix embedding spaces — fix the env, or migrate deliberately "
            f"(re-embed the corpus, recalibrate thresholds, update the meta row)."
        )
        raise RuntimeError(msg)
    _space_verified = True


def embed(texts: list[str]) -> np.ndarray:
    """Embed *texts* into a `(len(texts), 1024)` float32 array.

    Output is L2-normalized so Milvus' `COSINE` metric reduces to inner
    product. Returns a zero-row array when *texts* is empty so callers can
    pass through `np.ndarray` without a None branch — without touching the
    network, the database, or the key (pinned by tests since Phase 6).
    """
    if not texts:
        return np.zeros((0, EMBED_DIM), dtype=np.float32)

    _verify_embedding_space()
    arr = embed_texts(texts, model=inference_settings.embeddings_model)
    if arr.shape != (len(texts), EMBED_DIM):
        msg = (
            f"Remote embeddings returned shape {arr.shape}; expected "
            f"({len(texts)}, {EMBED_DIM}). Model swap or provider drift?"
        )
        raise RuntimeError(msg)
    return arr
