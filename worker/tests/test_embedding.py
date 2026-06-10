"""Tests for the remote BGE-Large embedder (Phase 6 contract, Phase 16b seam).

Three layers:

1. **Pure unit** — empty input short-circuits without touching the
   network, the database, or the key. Runs everywhere, including CI.
   The transport seam (`inference.embed_texts`) and the embedding-space
   guard are monkeypatched so every Phase 6 behavioral pin (shape,
   dtype, the shape-drift RuntimeError) survives the Phase 16b swap.
2. **Guard unit** — the embedding-space guard refuses to embed when
   `SERMON_EMBEDDINGS_MODEL` disagrees with the `meta` row, records the
   configured model on a missing row, and latches per process. Driven
   through a fake session factory — no Postgres.
3. **Live** — call the real DeepInfra endpoint (skipped without
   `DEEPINFRA_API_KEY`): shape, dtype, L2 normalization, and the
   load-bearing Phase 16b claim — REMOTE VECTORS MATCH THE LOCAL
   MODEL'S within float tolerance (`tests/golden/local_model_refvecs.npz`
   was captured from the in-process sentence-transformers loaders on
   2026-06-05, immediately before their removal). If the parity test
   regresses, the provider is no longer serving the exact weights and
   every stored Milvus vector + calibrated threshold is suspect.
"""

# Tests reach the guard internals on purpose; autouse fixtures look unused
# to the type-checker.
# pyright: reportPrivateUsage=false, reportUnusedFunction=false

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import embedding as embedding_module
import inference as inference_module
from db import Meta
from embedding import EMBED_DIM, embed

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
REFVECS_PATH = GOLDEN_DIR / "local_model_refvecs.npz"

# Remote fp16/bf16 inference vs the local fp32 reference: cosine must be
# essentially 1; elementwise drift stays well under these bounds when the
# weights are identical (and blows far past them when they are not).
_PARITY_MIN_COSINE = 0.999
_PARITY_ATOL = 0.015


def _key_available() -> bool:
    return bool(os.environ.get("DEEPINFRA_API_KEY"))


@pytest.fixture(autouse=True)
def _reset_space_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with the once-per-process guard latch cleared."""
    monkeypatch.setattr(embedding_module, "_space_verified", False)


def _bypass_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embedding_module, "_verify_embedding_space", lambda: None)


def _zeros_transport(dim: int) -> Any:  # noqa: ANN401
    """A fake ``embed_texts`` returning all-zero rows of width *dim*."""

    def _fake(texts: list[str], **_kwargs: Any) -> np.ndarray:  # noqa: ANN401
        return np.zeros((len(texts), dim), dtype=np.float32)

    return _fake


# --- pure unit ---------------------------------------------------------------


def test_embed_empty_returns_zero_rows_no_network_no_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty input must touch neither the transport nor the guard.

    Guards CI (and the api's empty-library path) from needing a key or
    a database when nothing needs embedding — the Phase 6 pin, upgraded
    from "no model download" to "no network, no DB".
    """

    def _explode(*_args: Any, **_kwargs: Any) -> Any:  # noqa: ANN401
        msg = "embed([]) must not reach the transport or the guard"
        raise AssertionError(msg)

    monkeypatch.setattr(embedding_module, "embed_texts", _explode)
    monkeypatch.setattr(embedding_module, "_verify_embedding_space", _explode)
    out = embed([])
    assert out.shape == (0, EMBED_DIM)
    assert out.dtype == np.float32


def test_embed_returns_transport_rows_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed() passes texts through and returns the transport's rows as-is."""
    _bypass_guard(monkeypatch)
    rows = np.eye(3, EMBED_DIM, dtype=np.float32)
    seen: list[list[str]] = []

    def _fake(texts: list[str], *, model: str) -> np.ndarray:
        seen.append(texts)
        assert model == embedding_module.MODEL_NAME
        return rows

    monkeypatch.setattr(embedding_module, "embed_texts", _fake)
    out = embed(["a", "b", "c"])
    assert seen == [["a", "b", "c"]]
    assert np.array_equal(out, rows)


def test_embed_raises_on_shape_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong-dimension response is a loud RuntimeError, not silent corruption.

    Same pin as the in-process era: if the provider swaps the model (or
    pads dimensions), every downstream Milvus insert/search would be
    garbage — fail before any of that.
    """
    _bypass_guard(monkeypatch)
    monkeypatch.setattr(embedding_module, "embed_texts", _zeros_transport(768))
    with pytest.raises(RuntimeError, match="shape"):
        embed(["a", "b"])


# --- embedding-space guard ---------------------------------------------------


class _FakeSession:
    """Minimal stand-in for a sync SQLAlchemy session (get/execute only)."""

    def __init__(self, row: Meta | None) -> None:
        self._row = row
        self.executed: list[object] = []

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def begin(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    def get(self, model: type[Meta], key: str) -> Meta | None:
        assert model is Meta
        assert key == "embedding_model_id"
        return self._row

    def execute(self, stmt: object) -> None:
        self.executed.append(stmt)


def _install_fake_session(
    monkeypatch: pytest.MonkeyPatch,
    row: Meta | None,
) -> _FakeSession:
    session = _FakeSession(row)
    monkeypatch.setattr(embedding_module, "get_sync_session_factory", lambda: lambda: session)
    return session


def test_guard_passes_and_latches_when_models_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recorded == configured → embed proceeds; second call skips the DB."""
    configured = inference_module.settings.embeddings_model
    session = _install_fake_session(monkeypatch, Meta(key="embedding_model_id", value=configured))
    monkeypatch.setattr(embedding_module, "embed_texts", _zeros_transport(EMBED_DIM))

    embed(["x"])
    assert session.executed == []  # no insert needed
    # Latch: break the session factory; a second embed must not touch it.
    monkeypatch.setattr(
        embedding_module,
        "get_sync_session_factory",
        lambda: (_ for _ in ()).throw(AssertionError("guard must be latched")),
    )
    embed(["y"])


def test_guard_refuses_on_model_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recorded != configured → RuntimeError; the transport is never reached.

    THE Phase 16b safety property: silent provider/model drift would mix
    embedding spaces and quietly destroy retrieval — it must be loud.
    """
    _install_fake_session(monkeypatch, Meta(key="embedding_model_id", value="some/other-model"))

    def _explode(*_args: Any, **_kwargs: Any) -> Any:  # noqa: ANN401
        msg = "transport must not be reached on a space mismatch"
        raise AssertionError(msg)

    monkeypatch.setattr(embedding_module, "embed_texts", _explode)
    with pytest.raises(RuntimeError, match="Embedding-space mismatch"):
        embed(["x"])
    assert embedding_module._space_verified is False


def test_guard_records_configured_model_on_missing_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-0003 database gets the configured model recorded, then proceeds."""
    session = _install_fake_session(monkeypatch, None)
    monkeypatch.setattr(embedding_module, "embed_texts", _zeros_transport(EMBED_DIM))

    embed(["x"])
    assert len(session.executed) == 1  # the ON CONFLICT DO NOTHING insert
    assert embedding_module._space_verified is True


# --- live (requires DEEPINFRA_API_KEY) ----------------------------------------


@pytest.mark.skipif(not _key_available(), reason="DEEPINFRA_API_KEY unset — live test skipped")
def test_live_embed_texts_shape_dtype_and_normalization() -> None:
    """Real endpoint returns (N, 1024) float32 unit vectors.

    Exercises the transport directly (no guard → no Postgres needed):
    ||v|| ≈ 1 is the precondition for Milvus' COSINE metric to behave as
    inner-product — ARCHITECTURE.md §3.
    """
    texts = [
        "Grace and peace to you from God our Father.",
        "Justification by faith alone.",
        "And the Word became flesh and dwelt among us.",
    ]
    out = inference_module.embed_texts(texts, model=inference_module.settings.embeddings_model)
    assert out.shape == (len(texts), EMBED_DIM)
    assert out.dtype == np.float32
    norms = np.linalg.norm(out, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5), f"norms={norms!r}"


@pytest.mark.skipif(not _key_available(), reason="DEEPINFRA_API_KEY unset — live test skipped")
@pytest.mark.parametrize("key", ["bge_large", "bge_m3"])
def test_live_remote_vectors_match_local_model_reference(key: str) -> None:
    """Same weights ⇒ same vectors — the claim Phase 16b stands on.

    The reference vectors were produced by the in-process
    sentence-transformers loaders right before their removal. If this
    fails, the provider is NOT serving the exact weights: stored Milvus
    vectors, golden min_score floors, and the highlight 0.5 threshold
    are all suspect. Do not loosen the tolerances to make it pass.
    """
    ref = np.load(REFVECS_PATH)
    texts = [str(t) for t in ref[f"{key}_texts"]]
    expected = np.asarray(ref[key], dtype=np.float32)
    model = inference_module.settings.embeddings_model if key == "bge_large" else "BAAI/bge-m3"

    out = inference_module.embed_texts(texts, model=model)
    assert out.shape == expected.shape
    cosines = np.sum(out * expected, axis=1)  # both sides unit-normalized
    assert np.all(cosines >= _PARITY_MIN_COSINE), f"cosines={cosines!r}"
    assert np.allclose(out, expected, atol=_PARITY_ATOL)
