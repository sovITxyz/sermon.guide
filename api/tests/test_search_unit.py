"""Unit tests for retrieval helpers — no DB, no Milvus, no model load.

Live retrieval is covered by ``worker/tests/test_retrieval_golden.py``;
this file pins the small, deterministic pieces of ``worker/retrieval.py``
that the API depends on so regressions in filter-expression shape,
RRF fusion math, or short-circuit semantics surface without requiring
infra.

Phase 22 adds the ``run_search`` graceful-degradation contract, every
I/O seam monkeypatched (``search._dense_arm`` / ``search.bm25_search`` /
``search.rerank`` / ``search.highlight``):

- One retrieval arm down → the surviving arm's results + the failed arm
  named in ``degraded`` — and the surviving arm ran with the exact
  JWT-derived ``book_id`` set resolved before the fan-out (degradation
  never widens scope).
- Both arms down → 503 with the fixed detail (never the exception text).
- Rerank failure → raw RRF top-K fallback + ``"rerank"`` flag, and
  highlight STILL runs on the RRF-ordered fallback.
- Highlight failure → reranked hits pass through unpruned + flag.
- Cancellation is flow control, never swallowed into a degraded response.
"""

# Tests exercise module-internals on purpose. ``pytest.approx`` ships
# loose stubs that pyright strict reports as Unknown — silence per-file
# (pymilvus ships no ``py.typed`` either; same relaxations as search.py).
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fastapi import HTTPException
from inference import RemoteInferenceError
from pydantic import ValidationError
from pymilvus import MilvusException
from retrieval import RetrievalHit, _build_milvus_filter, rrf_fuse

import search as search_module
from search import SearchOutcome, SearchRequest


def test_build_milvus_filter_quotes_each_uuid() -> None:
    a = uuid.UUID("11111111-1111-1111-1111-111111111111")
    b = uuid.UUID("22222222-2222-2222-2222-222222222222")
    expr = _build_milvus_filter([a, b])
    # Milvus expects `book_id in ["uuid1", "uuid2"]`; the exact string is
    # the contract between the API and the partition-key filter.
    expected = (
        'book_id in ["11111111-1111-1111-1111-111111111111", '
        '"22222222-2222-2222-2222-222222222222"]'
    )
    assert expr == expected


def test_build_milvus_filter_rejects_empty() -> None:
    # An empty filter list would either become `book_id in []` (which some
    # pymilvus builds reject) or — worse — an accidentally-unfiltered
    # search if the caller stripped the clause. The endpoint short-circuits
    # the request before calling this helper; we enforce that contract
    # here so a future caller can't bypass it silently.
    with pytest.raises(ValueError, match="at least one book_id"):
        _build_milvus_filter([])


def test_build_milvus_filter_single_book_id() -> None:
    bid = uuid.UUID("33333333-3333-3333-3333-333333333333")
    assert _build_milvus_filter([bid]) == 'book_id in ["33333333-3333-3333-3333-333333333333"]'


def _hit(book_id: uuid.UUID, chunk_index: int, *, score: float = 1.0) -> RetrievalHit:
    return RetrievalHit(
        book_id=book_id,
        chunk_index=chunk_index,
        content_chunk=f"chunk-{chunk_index}",
        metadata={"chunk_index": chunk_index},
        score=score,
        dense_score=score,
    )


def test_rrf_fuse_sums_both_arms_for_shared_hit() -> None:
    """A chunk present in both arms gets 1/(k+rank_dense) + 1/(k+rank_sparse)."""
    bid = uuid.UUID("44444444-4444-4444-4444-444444444444")
    # Same chunk at dense-rank-1 and sparse-rank-2.
    dense = [_hit(bid, 0, score=0.9)]
    sparse = [
        RetrievalHit(
            book_id=uuid.UUID("55555555-5555-5555-5555-555555555555"),
            chunk_index=7,
            content_chunk="other",
            metadata={"chunk_index": 7},
            score=0.4,
            sparse_score=0.4,
        ),
        RetrievalHit(
            book_id=bid,
            chunk_index=0,
            content_chunk="chunk-0",
            metadata={"chunk_index": 0},
            score=0.3,
            sparse_score=0.3,
        ),
    ]
    fused = rrf_fuse(dense=dense, sparse=sparse, limit=10, k=60)
    # Shared chunk: 1/61 (dense rank 1) + 1/62 (sparse rank 2) = 0.03251...
    # Other chunk: 1/61 (sparse rank 1)                          = 0.01639...
    assert len(fused) == 2
    assert fused[0].book_id == bid
    assert fused[0].dense_score == pytest.approx(0.9)
    assert fused[0].sparse_score == pytest.approx(0.3)
    assert fused[0].score == pytest.approx(1 / 61 + 1 / 62)
    # Solo entry from sparse arm keeps sparse_score, no dense_score.
    assert fused[1].dense_score is None
    assert fused[1].sparse_score == pytest.approx(0.4)
    assert fused[1].score == pytest.approx(1 / 61)


def test_rrf_fuse_orders_by_fused_score_desc() -> None:
    """Top hit is whichever has the highest summed reciprocal rank."""
    bid_a = uuid.UUID("66666666-6666-6666-6666-666666666666")
    bid_b = uuid.UUID("77777777-7777-7777-7777-777777777777")
    # bid_a appears only in dense at rank 1.
    # bid_b appears in both arms — beats bid_a even though bid_a is top of dense.
    dense = [_hit(bid_a, 0), _hit(bid_b, 3)]
    sparse = [
        RetrievalHit(
            book_id=bid_b,
            chunk_index=3,
            content_chunk="x",
            metadata={"chunk_index": 3},
            score=0.5,
            sparse_score=0.5,
        ),
    ]
    fused = rrf_fuse(dense=dense, sparse=sparse, limit=10, k=60)
    assert [h.book_id for h in fused] == [bid_b, bid_a]


def test_rrf_fuse_respects_limit() -> None:
    bids = [uuid.uuid4() for _ in range(5)]
    dense = [_hit(b, i) for i, b in enumerate(bids)]
    sparse: list[RetrievalHit] = []
    fused = rrf_fuse(dense=dense, sparse=sparse, limit=3, k=60)
    assert len(fused) == 3
    # Still ordered by RRF (which mirrors dense order when sparse is empty).
    assert [h.book_id for h in fused] == bids[:3]


def test_rrf_fuse_empty_both_arms_returns_empty() -> None:
    assert rrf_fuse(dense=[], sparse=[], limit=10, k=60) == []


def test_search_request_forbids_extra_fields() -> None:
    """Phase 18: a smuggled ``user_id``/``book_ids`` is a hard 422 — the
    tenant scope comes from the JWT only (closes Phase 12 deviation d).
    ``model_validate`` because pyright already rejects unknown kwargs."""
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"query": "grace", "user_id": str(uuid.uuid4())})
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"query": "grace", "book_ids": [str(uuid.uuid4())]})


# --- run_search graceful degradation (Phase 22) ------------------------------


class _FakeScalars:
    def __init__(self, values: list[uuid.UUID]) -> None:
        self._values = values

    def all(self) -> list[uuid.UUID]:
        return self._values


class _FakeExecuteResult:
    def __init__(self, values: list[uuid.UUID]) -> None:
        self._values = values

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._values)


class _FakeSession:
    """Duck-typed ``AsyncSession``: answers the user_library book_id query."""

    def __init__(self, book_ids: list[uuid.UUID]) -> None:
        self.book_ids = book_ids

    async def execute(self, _stmt: Any) -> _FakeExecuteResult:  # noqa: ANN401
        return _FakeExecuteResult(self.book_ids)


# The JWT user's resolved library — what BOTH arms must be scoped to.
_LIBRARY = [uuid.UUID(int=101), uuid.UUID(int=102)]


async def _run(
    *,
    limit: int = 10,
    do_rerank: bool = False,
    book_ids: list[uuid.UUID] | None = None,
) -> SearchOutcome:
    session: Any = _FakeSession(_LIBRARY if book_ids is None else book_ids)
    return await search_module.run_search(
        query="q",
        limit=limit,
        do_rerank=do_rerank,
        user_id=uuid.uuid4(),
        session=session,
    )


async def test_run_search_empty_library_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _explode(*_args: Any, **_kwargs: Any) -> list[RetrievalHit]:
        pytest.fail("retrieval arm must not run for an empty library")

    monkeypatch.setattr(search_module, "_dense_arm", _explode)
    monkeypatch.setattr(search_module, "bm25_search", _explode)

    outcome = await _run(book_ids=[])
    assert outcome.hits == []
    assert outcome.degraded == []


async def test_run_search_healthy_reports_no_degraded_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = uuid.UUID(int=1)

    async def _dense(_query: str, _book_ids: list[uuid.UUID]) -> list[RetrievalHit]:
        return [_hit(a, 0, score=0.9)]

    async def _sparse(**_: Any) -> list[RetrievalHit]:
        return [_hit(a, 1, score=0.5)]

    monkeypatch.setattr(search_module, "_dense_arm", _dense)
    monkeypatch.setattr(search_module, "bm25_search", _sparse)

    outcome = await _run()
    assert outcome.degraded == []
    assert len(outcome.hits) == 2


async def test_run_search_dense_down_degrades_to_sparse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def _dense(_query: str, _book_ids: list[uuid.UUID]) -> list[RetrievalHit]:
        raise MilvusException(message="connection refused: milvus:19530")

    async def _sparse(**kwargs: Any) -> list[RetrievalHit]:
        seen["book_ids"] = list(kwargs["book_ids"])
        return [_hit(uuid.UUID(int=1), 3, score=0.4)]

    monkeypatch.setattr(search_module, "_dense_arm", _dense)
    monkeypatch.setattr(search_module, "bm25_search", _sparse)

    outcome = await _run()
    assert outcome.degraded == ["dense"]
    assert [h.metadata["chunk_index"] for h in outcome.hits] == [3]
    # Tenant pin: the surviving arm ran with the exact JWT-derived library
    # set resolved once before the fan-out — degradation cannot widen scope.
    assert seen["book_ids"] == _LIBRARY


async def test_run_search_sparse_down_degrades_to_dense(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def _dense(_query: str, book_ids: list[uuid.UUID]) -> list[RetrievalHit]:
        seen["book_ids"] = list(book_ids)
        return [_hit(uuid.UUID(int=2), 5, score=0.8)]

    async def _sparse(**_: Any) -> list[RetrievalHit]:
        msg = "postgres unreachable"
        raise ConnectionRefusedError(msg)

    monkeypatch.setattr(search_module, "_dense_arm", _dense)
    monkeypatch.setattr(search_module, "bm25_search", _sparse)

    outcome = await _run()
    assert outcome.degraded == ["sparse"]
    assert [h.metadata["chunk_index"] for h in outcome.hits] == [5]
    assert seen["book_ids"] == _LIBRARY


async def test_run_search_both_arms_down_is_503_with_clean_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _dense(_query: str, _book_ids: list[uuid.UUID]) -> list[RetrievalHit]:
        raise MilvusException(message="connection refused: milvus:19530")

    async def _sparse(**_: Any) -> list[RetrievalHit]:
        msg = "postgres://user:hunter2@db:5432 unreachable"
        raise ConnectionRefusedError(msg)

    monkeypatch.setattr(search_module, "_dense_arm", _dense)
    monkeypatch.setattr(search_module, "bm25_search", _sparse)

    with pytest.raises(HTTPException) as excinfo:
        await _run()
    assert excinfo.value.status_code == 503
    # The body is the fixed message — never the exceptions (which can embed
    # hosts and DSNs; the /readyz never-body-the-failure rule).
    detail = str(excinfo.value.detail)
    assert detail == search_module._RETRIEVAL_UNAVAILABLE_DETAIL
    assert "milvus" not in detail.lower()
    assert "hunter2" not in detail


async def test_run_search_cancellation_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CancelledError is flow control (client disconnect / shutdown), not a
    dependency failure — it must propagate, never become a degraded arm."""

    async def _dense(_query: str, _book_ids: list[uuid.UUID]) -> list[RetrievalHit]:
        raise asyncio.CancelledError

    async def _sparse(**_: Any) -> list[RetrievalHit]:
        return []

    monkeypatch.setattr(search_module, "_dense_arm", _dense)
    monkeypatch.setattr(search_module, "bm25_search", _sparse)

    with pytest.raises(asyncio.CancelledError):
        await _run()


def _arms_with_dense_hits(
    monkeypatch: pytest.MonkeyPatch,
    hits: list[RetrievalHit],
) -> None:
    """Healthy arms: dense returns *hits*, sparse returns nothing — so the
    RRF order deterministically mirrors the dense order."""

    async def _dense(_query: str, _book_ids: list[uuid.UUID]) -> list[RetrievalHit]:
        return hits

    async def _sparse(**_: Any) -> list[RetrievalHit]:
        return []

    monkeypatch.setattr(search_module, "_dense_arm", _dense)
    monkeypatch.setattr(search_module, "bm25_search", _sparse)


async def test_run_search_rerank_failure_falls_back_to_rrf_and_still_highlights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b, c = uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3)
    _arms_with_dense_hits(monkeypatch, [_hit(a, 0), _hit(b, 1), _hit(c, 2)])

    def _boom_rerank(**_: Any) -> list[RetrievalHit]:
        msg = "DeepInfra rerank call failed: timeout"
        raise RemoteInferenceError(msg)

    highlighted: dict[str, Any] = {}

    def _fake_highlight(*, query: str, hits: Any) -> list[RetrievalHit]:
        highlighted["query"] = query
        highlighted["hits"] = list(hits)
        return list(hits)

    monkeypatch.setattr(search_module, "rerank", _boom_rerank)
    monkeypatch.setattr(search_module, "highlight", _fake_highlight)

    outcome = await _run(limit=2, do_rerank=True)
    assert outcome.degraded == ["rerank"]
    # Raw RRF top-K passthrough — the same order + truncation rerank=false
    # would have returned.
    assert [h.book_id for h in outcome.hits] == [a, b]
    # Highlight STILL ran, on the RRF-ordered fallback (a rerank failure
    # must not skip pruning — highlight needs only the query + a hit list).
    assert [h.book_id for h in highlighted["hits"]] == [a, b]
    assert highlighted["query"] == "q"


async def test_run_search_highlight_failure_keeps_reranked_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = uuid.UUID(int=1), uuid.UUID(int=2)
    _arms_with_dense_hits(monkeypatch, [_hit(a, 0), _hit(b, 1)])

    def _fake_rerank(**kwargs: Any) -> list[RetrievalHit]:
        hits: list[RetrievalHit] = list(kwargs["hits"])
        top_n: int = kwargs["top_n"]
        return list(reversed(hits))[:top_n]

    def _boom_highlight(**_: Any) -> list[RetrievalHit]:
        msg = "DeepInfra embeddings call failed: timeout"
        raise RemoteInferenceError(msg)

    monkeypatch.setattr(search_module, "rerank", _fake_rerank)
    monkeypatch.setattr(search_module, "highlight", _boom_highlight)

    outcome = await _run(limit=2, do_rerank=True)
    assert outcome.degraded == ["highlight"]
    # The reranked order survives, content unpruned.
    assert [h.book_id for h in outcome.hits] == [b, a]
    assert [h.content_chunk for h in outcome.hits] == ["chunk-1", "chunk-0"]


async def test_run_search_flags_accumulate_in_pipeline_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arm + stage failures in one request stack up: dense, sparse, rerank,
    highlight — stable, counter-friendly ordering for Phase 27."""

    async def _dense(_query: str, _book_ids: list[uuid.UUID]) -> list[RetrievalHit]:
        raise MilvusException(message="connection refused")

    async def _sparse(**_: Any) -> list[RetrievalHit]:
        return [_hit(uuid.UUID(int=1), 0, score=0.4)]

    def _boom_rerank(**_: Any) -> list[RetrievalHit]:
        msg = "rerank down"
        raise RemoteInferenceError(msg)

    def _fake_highlight(*, query: str, hits: Any) -> list[RetrievalHit]:
        del query
        return list(hits)

    monkeypatch.setattr(search_module, "_dense_arm", _dense)
    monkeypatch.setattr(search_module, "bm25_search", _sparse)
    monkeypatch.setattr(search_module, "rerank", _boom_rerank)
    monkeypatch.setattr(search_module, "highlight", _fake_highlight)

    outcome = await _run(limit=5, do_rerank=True)
    assert outcome.degraded == ["dense", "rerank"]
    assert len(outcome.hits) == 1


def test_search_response_degraded_defaults_to_empty() -> None:
    """The Phase 22 field is additive: always present, ``[]`` when healthy."""
    resp = search_module.SearchResponse(hits=[])
    assert resp.degraded == []
    assert resp.model_dump()["degraded"] == []


async def test_search_handler_carries_degraded_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_run_search(**_: Any) -> SearchOutcome:  # noqa: ANN401
        return SearchOutcome(hits=[], degraded=["dense"])

    monkeypatch.setattr(search_module, "run_search", _fake_run_search)
    user: Any = type("U", (), {"user_id": uuid.uuid4()})()
    session: Any = object()

    resp = await search_module.search(
        payload=SearchRequest(query="q"),
        current_user=user,
        session=session,
    )
    assert resp.degraded == ["dense"]
    assert resp.hits == []
