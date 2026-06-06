"""Unit tests for the remote inference transport (Phase 16b, ADR 0006).

Everything mocked — no key, no network. Pins the transport contracts the
rest of the pipeline stands on:

- embeddings: batching below the provider cap, strict input-order
  reassembly (sorted by the response's ``index``, not response order),
  client-side L2 normalization, row-count validation, the
  ``openai.APIError`` → ``RemoteInferenceError`` mapping, and the
  empty-input no-network short-circuit.
- rerank: query replication + document order on the wire, auth header
  shape, exactly-one-retry on 5xx/transport errors, NO retry on 4xx,
  and malformed-body rejection (missing/short/non-numeric/boolean
  scores).
- both legs: unset ``DEEPINFRA_API_KEY`` → ``MissingInferenceKeyError``
  (the api maps it to 503; ``RemoteInferenceError`` maps to 502).
"""

# Tests reach into module internals (the client cache, the settings
# singleton) on purpose; autouse fixtures look unused to the type-checker.
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnusedFunction=false

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import numpy as np
import openai
import pytest

import inference as inference_module
from inference import (
    MissingInferenceKeyError,
    RemoteInferenceError,
    embed_texts,
    rerank_scores,
)


@pytest.fixture(autouse=True)
def _fresh_client_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets a configured key and an empty client cache."""
    monkeypatch.setattr(inference_module.settings, "deepinfra_api_key", "test-key")
    inference_module._embeddings_client.cache_clear()


# --- fakes --------------------------------------------------------------------


@dataclass
class _FakeEmbeddingRow:
    index: int
    embedding: list[float]


@dataclass
class _FakeEmbeddingResponse:
    data: list[_FakeEmbeddingRow]


class _FakeEmbeddingsAPI:
    """Stand-in for ``client.embeddings`` recording create() calls."""

    def __init__(self, responder: Any) -> None:  # noqa: ANN401
        self._responder = responder
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeEmbeddingResponse:  # noqa: ANN401
        self.calls.append(kwargs)
        return self._responder(kwargs)


@dataclass
class _FakeOpenAIClient:
    embeddings: _FakeEmbeddingsAPI


def _install_embeddings(monkeypatch: pytest.MonkeyPatch, responder: Any) -> _FakeEmbeddingsAPI:  # noqa: ANN401
    api = _FakeEmbeddingsAPI(responder)
    monkeypatch.setattr(
        inference_module,
        "_embeddings_client",
        lambda: _FakeOpenAIClient(embeddings=api),
    )
    return api


@dataclass
class _FakeHTTPPost:
    """Stand-in for ``httpx.post`` returning queued responses/exceptions."""

    queue: list[httpx.Response | Exception]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, url: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        self.calls.append({"url": url, **kwargs})
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _response(status_code: int, body: Any = None) -> httpx.Response:  # noqa: ANN401
    return httpx.Response(
        status_code,
        json=body,
        request=httpx.Request("POST", "https://api.deepinfra.com/v1/inference/x"),
    )


# --- embeddings ----------------------------------------------------------------


def test_embed_texts_empty_returns_zero_rows_without_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode() -> Any:  # noqa: ANN401
        msg = "client must not be constructed for empty input"
        raise AssertionError(msg)

    monkeypatch.setattr(inference_module, "_embeddings_client", _explode)
    out = embed_texts([], model="m")
    assert out.shape == (0, 0)


def test_embed_texts_normalizes_and_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows come back L2-normalized and aligned with the input order —
    even when the provider returns them shuffled (we sort by index).

    Row i rides a DISTINCT axis (e_i scaled by i+1) so the index re-sort
    is genuinely exercised: normalization kills magnitude but not
    direction, so dropping the sort would leave the reversed rows in
    place and fail the identity-matrix assertion below. (The first cut
    of this test put every row on axis 0 — normalization collapsed them
    to identical vectors and the sort was unpinned; adversarial review
    2026-06-05.)
    """

    def _responder(kwargs: dict[str, Any]) -> _FakeEmbeddingResponse:
        texts: list[str] = kwargs["input"]
        n = len(texts)
        # Row i → (i+1)·e_i, returned REVERSED to prove we re-sort by index.
        rows = [
            _FakeEmbeddingRow(
                index=i,
                embedding=[float(i + 1) if axis == i else 0.0 for axis in range(n)],
            )
            for i in range(n)
        ]
        return _FakeEmbeddingResponse(data=list(reversed(rows)))

    api = _install_embeddings(monkeypatch, _responder)
    out = embed_texts(["a", "b", "c"], model="m")

    assert api.calls[0]["model"] == "m"
    assert api.calls[0]["input"] == ["a", "b", "c"]
    assert out.shape == (3, 3)
    assert out.dtype == np.float32
    assert np.allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-6)
    # Row i must be the unit vector along axis i — true only if the
    # reversed response was re-sorted by its index field.
    assert np.allclose(out, np.eye(3, dtype=np.float32), atol=1e-6)


def test_embed_texts_batches_below_provider_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inputs beyond _MAX_BATCH_ITEMS split into ordered batches."""
    n = inference_module._MAX_BATCH_ITEMS + 3

    def _responder(kwargs: dict[str, Any]) -> _FakeEmbeddingResponse:
        texts: list[str] = kwargs["input"]
        # Encode the global position (parsed from the text) into the vector
        # so cross-batch order is verifiable after reassembly.
        return _FakeEmbeddingResponse(
            data=[
                _FakeEmbeddingRow(index=i, embedding=[float(t), 1.0]) for i, t in enumerate(texts)
            ],
        )

    api = _install_embeddings(monkeypatch, _responder)
    out = embed_texts([str(i) for i in range(n)], model="m")

    assert [len(c["input"]) for c in api.calls] == [inference_module._MAX_BATCH_ITEMS, 3]
    assert out.shape == (n, 2)
    # Unnormalized first components were 0..n-1; normalization preserves
    # monotonicity of x/sqrt(x²+1), so order survives the reassembly.
    firsts = out[:, 0].tolist()
    assert firsts == sorted(firsts)


def test_embed_texts_maps_api_error_to_remote_inference_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _responder(_kwargs: dict[str, Any]) -> _FakeEmbeddingResponse:
        raise openai.APIError(
            "down",
            httpx.Request("POST", "https://api.deepinfra.com/v1/openai/embeddings"),
            body=None,
        )

    _install_embeddings(monkeypatch, _responder)
    with pytest.raises(RemoteInferenceError, match="embeddings"):
        embed_texts(["a"], model="m")


def test_embed_texts_rejects_row_count_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    def _responder(_kwargs: dict[str, Any]) -> _FakeEmbeddingResponse:
        return _FakeEmbeddingResponse(data=[_FakeEmbeddingRow(index=0, embedding=[1.0])])

    _install_embeddings(monkeypatch, _responder)
    with pytest.raises(RemoteInferenceError, match="rows"):
        embed_texts(["a", "b"], model="m")


def test_embed_texts_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inference_module.settings, "deepinfra_api_key", None)
    with pytest.raises(MissingInferenceKeyError, match="DEEPINFRA_API_KEY"):
        embed_texts(["a"], model="m")


# --- rerank --------------------------------------------------------------------


def test_rerank_scores_empty_documents_no_http(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeHTTPPost(queue=[])
    monkeypatch.setattr(inference_module.httpx, "post", fake)
    assert rerank_scores(query="q", documents=[]) == []
    assert fake.calls == []


def test_rerank_scores_wire_shape_and_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Query replicated per document, documents in order, bearer auth,
    model id in the URL path, scores returned as floats."""
    fake = _FakeHTTPPost(queue=[_response(200, {"scores": [0.9, 0.1]})])
    monkeypatch.setattr(inference_module.httpx, "post", fake)

    out = rerank_scores(query="q", documents=["d1", "d2"])

    assert out == [0.9, 0.1]
    call = fake.calls[0]
    assert call["url"].endswith(f"/{inference_module.settings.rerank_model}")
    assert call["json"] == {"queries": ["q", "q"], "documents": ["d1", "d2"]}
    assert call["headers"]["Authorization"] == "Bearer test-key"


def test_rerank_scores_retries_once_on_5xx_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeHTTPPost(queue=[_response(500), _response(200, {"scores": [0.5]})])
    monkeypatch.setattr(inference_module.httpx, "post", fake)
    assert rerank_scores(query="q", documents=["d"]) == [0.5]
    assert len(fake.calls) == 2


def test_rerank_scores_retries_once_on_transport_error_then_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two transport failures exhaust the single retry → RemoteInferenceError."""
    fake = _FakeHTTPPost(
        queue=[httpx.ConnectError("boom"), httpx.ReadTimeout("slow")],
    )
    monkeypatch.setattr(inference_module.httpx, "post", fake)
    with pytest.raises(RemoteInferenceError, match="rerank"):
        rerank_scores(query="q", documents=["d"])
    assert len(fake.calls) == 2


def test_rerank_scores_no_retry_on_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4xx (bad key, bad request) will not heal — fail immediately."""
    fake = _FakeHTTPPost(queue=[_response(401)])
    monkeypatch.setattr(inference_module.httpx, "post", fake)
    with pytest.raises(RemoteInferenceError, match="HTTP 401"):
        rerank_scores(query="q", documents=["d"])
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"not_scores": []},
        {"scores": [0.1]},  # short — two documents sent
        {"scores": [0.1, "high"]},
        {"scores": [0.1, True]},  # bool is an int subclass; still malformed
        [0.1, 0.2],  # top level not a dict
    ],
)
def test_rerank_scores_rejects_malformed_bodies(
    monkeypatch: pytest.MonkeyPatch,
    body: Any,  # noqa: ANN401
) -> None:
    fake = _FakeHTTPPost(queue=[_response(200, body)])
    monkeypatch.setattr(inference_module.httpx, "post", fake)
    with pytest.raises(RemoteInferenceError, match="malformed"):
        rerank_scores(query="q", documents=["d1", "d2"])


def test_rerank_scores_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inference_module.settings, "deepinfra_api_key", None)
    with pytest.raises(MissingInferenceKeyError, match="DEEPINFRA_API_KEY"):
        rerank_scores(query="q", documents=["d"])
