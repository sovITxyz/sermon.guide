"""Authenticated hybrid + rerank + highlight search over the JWT user's library.

``POST /search`` runs dense (BGE-Large → Milvus COSINE) and sparse
(Postgres ``tsvector`` → ``ts_rank_cd``) retrieval in parallel and
fuses them via Reciprocal Rank Fusion (RRF, k=60). When
``payload.rerank`` is true (the default), the top-30 fused hits are
reranked by a cross-encoder (Phase 13, ``api/rerank.py``) and pruned
sentence-by-sentence against the query via BGE-M3 semantic
highlighting (``api/highlight.py``); when false, the raw RRF top-K
flows through unchanged (Phase 12 behavior).

The algorithm primitives live elsewhere — this module is the FastAPI
wrapper that handles auth, request validation, and the event-loop
dance between sync (Milvus, the blocking remote-inference calls) and
async (SQLAlchemy ``AsyncSession``) call sites. Since Phase 16b
(ADR 0006) every inference leg — query embedding, rerank, highlight —
is a remote API call through ``worker/inference.py``; no model weights
load in this process.

## Trust boundary

This is the load-bearing tenant invariant for retrieval (repo-root
``CLAUDE.md`` + ``ARCHITECTURE.md`` §3 + §7.1):

- ``user_id`` is **always** ``current_user.user_id`` from the JWT — never
  read from the request body or query. A search payload field named
  ``user_id`` is an automatic reject.
- The ``book_id`` set used by both retrieval arms is resolved
  server-side from ``user_library`` for that JWT ``user_id`` on every
  request — that resolved library is the *universe*. A client MAY now
  (Phase 49) narrow the search with ``book_ids`` / ``collection_ids``,
  but the effective set is ALWAYS the INTERSECTION of the request with
  that library (``effective = requested & library``): scope can only
  SHRINK, never widen. ``collection_ids`` are ownership-checked against
  the JWT user (a foreign/nonexistent id is a no-oracle 404) before
  their member books join the requested set. Omitting both fields
  searches the whole library (backward compatible).
- Every Milvus search includes ``book_id IN (<set>)`` as the filter
  expression; every BM25 search includes ``book_id = ANY(<set>)`` in
  the WHERE clause. An empty library short-circuits to an empty
  response *before* embedding so we don't run the model on a request
  that can't return anything; we also never issue a ``book_id in []``
  filter (some pymilvus builds reject it, and the semantics are
  ambiguous anyway).
- Rerank + highlight (Phase 13) are post-retrieval stages: they only
  see hits that already cleared the tenant filter on both arms. They
  do not query the DB or Milvus, so they introduce no new tenant
  surface. Since Phase 16b they send the user query + chunk content to
  the remote inference provider as model input — text the JWT user was
  already authorized to read, never ``user_id``/JWT/email (see
  ``worker/inference.py`` tenant notes) — not a SQL / filter-expression
  injection vector.

## Parallelism

The async handler kicks off two concurrent retrieval tasks via
``asyncio.gather``:

1. Embed the query (``asyncio.to_thread`` around the blocking remote
   embeddings call), then run the Milvus search (also ``to_thread`` —
   pymilvus is blocking).
2. Run the BM25 search directly on the request's ``AsyncSession``.

After fusion, the rerank + highlight stages run sequentially on the
fused list, each in ``asyncio.to_thread`` (both are blocking remote
calls). Sequential because highlight depends on the post-rerank top-N;
parallelism within the post-retrieval pipeline would only save the
smaller stage's wall time, and the implementation cost (more failure
modes for ``asyncio.gather`` to fan out) isn't worth it at v0 scale.

## Process-level singletons

The remote-inference clients (``worker/inference.py``) and the Milvus
client are each constructed once per process, lazily on first use.
Since Phase 16b no model weights load here — a cold process's first
request pays connection setup, not multi-GB model loads.

## Why ``score`` shifts meaning by ``rerank``

The ``score`` field on each returned hit always conveys the ordering
score of the final ranking stage:

- ``rerank=false`` → RRF score (sum of reciprocal ranks across arms;
  same as Phase 12).
- ``rerank=true`` (default) → reranker relevance score (higher =
  better; roughly ``[0, 1]`` on the Phase 16b Qwen3 reranker).

The previous-stage scores survive on each hit's ``metadata``:
``rrf_score`` is written by the reranker; ``sentences_kept`` and
``sentences_total`` are written by the highlighter so callers can see
how aggressively each chunk was pruned without re-running BGE-M3.
Per-arm ``dense_score`` and ``sparse_score`` are on the internal
``RetrievalHit`` but are not in the public ``SearchHit`` schema —
clients see only the final ordering score and the chunk metadata.

## Failure mode — graceful degradation (Phase 22)

A single dependency blip no longer means a bare 500. The dense/sparse
fan-out runs with ``return_exceptions=True``: one arm down → the
surviving arm's (still tenant-scoped) results flow through fusion with
the failed arm named in the response's ``degraded`` list. Both arms
down → 503 with a fixed detail (a dependency outage is retryable and
NOT a bug, so it must not surface as a 500 stack; the detail never
carries the exception — connection errors can embed hosts/DSNs, the
Phase 18 never-body-the-failure rule).

Milvus-down is bounded by the arm budget, not by the RPC deadline alone:
the 2.5 s client/RPC deadlines (``worker/retrieval.py`` +
``MILVUS_TIMEOUT_SECONDS``) bound steady-state-down calls (sub-second
closed-channel fast-fail) but NOT a warm connection's first failure —
pymilvus 2.6 runs an in-request reconnect with a hardcoded 10 s wait
*before* its deadline check. The dense arm therefore wraps its whole
Milvus leg (client checkout + search RPC) in ``asyncio.wait_for`` under
``DENSE_ARM_BUDGET_SECONDS`` (4 s), so a Milvus-down request degrades in
≤4 s worst case; the budget-expired worker thread is orphaned, not
cancelled, and drains within pymilvus's own 10 s ceiling. On any
dense-arm ``MilvusException`` the process-wide client singleton is
dropped (``_reset_client``) so the next request reconstructs it — without
the reset a once-failed client stays dead past the outage, because
pymilvus closes the channel after a failed in-band recovery and never
retries the resulting non-gRPC errors. Recovery of pymilvus's cached
connection entry is additionally gated by its 30 s idle health-check
threshold, so the dense arm can stay (bounded-fast) degraded after Milvus
returns until construction attempts are ≥30 s apart.

Rerank and highlight each degrade independently: a
``RemoteInferenceError`` (the whole realistic failure surface since
Phase 16b — both stages are pure remote calls; anything else out of
them is a pipeline bug that should fail loud) falls back to the raw RRF
top-K — the same list ``rerank=false`` returns — flagged as
``"rerank"`` / ``"highlight"``. A rerank failure does NOT skip
highlight: highlight needs only a hit list and the query, so it still
prunes the RRF-ordered fallback (and if the failure was a provider
outage it simply degrades too — same provider, both flags appear).

Every degraded path is fail-loud in the logs (``exc_info``) and soft in
the response. Scope can never widen: the surviving arm already executed
with the request's JWT-derived ``book_id`` filter, and no fallback
re-queries anything — see ``run_search``.

Remote-inference failures OUTSIDE ``run_search`` are still mapped in
``api/main.py`` (Phase 16b): unset ``DEEPINFRA_API_KEY`` → 503 naming
the env var, upstream failure after retry → 502 naming the provider +
leg.
"""

# pymilvus 2.6 ships without `py.typed`; same relaxation as worker/.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnnecessaryComparison=false

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from db import UserLibraryEntry
from embedding import embed
from fastapi import APIRouter, HTTPException, status
from inference import RemoteInferenceError
from pydantic import BaseModel, ConfigDict, Field
from pymilvus import MilvusClient, MilvusException
from retrieval import (
    DENSE_FANOUT,
    SPARSE_FANOUT,
    RetrievalHit,
    bm25_search,
    dense_search,
    rrf_fuse,
)
from scripts.bootstrap_milvus import make_client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import CurrentUserDep, SessionDep

# Phase 49: intentionally-exported tenant-scope builders (collections_routes docstring).
from collections_routes import (
    _member_book_ids_stmt,  # pyright: ignore[reportPrivateUsage]
    _owned_collection_ids_stmt,  # pyright: ignore[reportPrivateUsage]
)
from highlight import highlight
from metrics import RETRIEVAL_DEGRADED, RETRIEVAL_STAGE
from rerank import RERANK_FANOUT, rerank

router = APIRouter(prefix="/search", tags=["search"])

logger = logging.getLogger(__name__)

# Hard ceiling on the dense arm's Milvus leg (client checkout + search RPC),
# enforced with ``asyncio.wait_for`` in ``_dense_arm``.
#
# Why it exists: ``MILVUS_TIMEOUT_SECONDS`` (2.5 s) does NOT bound a warm
# connection's FIRST failure. pymilvus 2.6's retry decorator calls its
# connection-recovery hook BEFORE the deadline check, and that hook runs an
# in-request reconnect with a HARDCODED 10 s channel-ready wait
# (``grpc_handler.reconnect(timeout=10)``) — live-measured at ~10-11 s for
# the first search after Milvus dies. 4 s clears the legitimate worst case
# (a fresh 2.5 s channel-ready wait at (re)construction, or a healthy search
# running up to its 2.5 s RPC deadline, plus thread-scheduling slack) while
# cutting that 10 s tail out of the request.
#
# Caveat — orphaned thread: ``wait_for`` cannot interrupt the ``to_thread``
# worker. On budget expiry the response degrades immediately but the thread
# keeps running (bounded by pymilvus's own 10 s reconnect ceiling); its
# eventual ``MilvusException`` still fires the singleton reset below, and an
# eventual success means the client was healthy-but-slow and is kept.
DENSE_ARM_BUDGET_SECONDS = 4.0

# Process-wide Milvus client. Lazily constructed so import-time doesn't
# require Milvus to be reachable (tests / type-check / lint shouldn't
# need a live broker).
#
# Phase 22: RESET on dense-arm ``MilvusException`` (``_reset_client``).
# After a failed in-band recovery pymilvus closes the gRPC channel and every
# later call raises a closed-channel error wrapped to ``MilvusException`` —
# never a ``grpc.RpcError`` — so pymilvus's UNAVAILABLE-driven recovery never
# refires and a pinned client stays dead PAST the outage (live-confirmed:
# dense never came back over 5+ min after Milvus restarted). Dropping the
# reference makes the next request reconstruct via ``make_client()``;
# construction is self-validating (``MilvusClient.__init__`` ends with a live
# ``get_server_type`` RPC), so a broken client can never be (re)published.
# While Milvus is down each rebuild attempt stays bounded: 2.5 s
# channel-ready wait on a fresh connection, sub-second closed-channel
# fast-fail on pymilvus's cached registry entry.
#
# Lock-free like the original singleton: the reference swap is atomic,
# ``make_client()`` fully constructs before publication (no half-built client
# is ever visible to another request), and the identity guard in
# ``_reset_client`` keeps a stale failure from clobbering a newer healthy
# client — the worst interleaving is a benign double-reset costing one extra
# reconstruct.
_milvus_client: MilvusClient | None = None


def _client() -> MilvusClient:
    global _milvus_client  # noqa: PLW0603 — module-level singleton, see module docstring
    if _milvus_client is None:
        _milvus_client = make_client()
    return _milvus_client


def _reset_client(failed: MilvusClient) -> None:
    """Drop the singleton iff it is still the client that just failed.

    The identity guard makes a late reset (e.g. from a budget-orphaned
    thread) a no-op once another request has already published a fresh
    client. Benign double-reset is acceptable; clobbering a healthy
    replacement is not.
    """
    global _milvus_client  # noqa: PLW0603 — module-level singleton, see module docstring
    if _milvus_client is failed:
        _milvus_client = None


class SearchRequest(BaseModel):
    """Search payload.

    No ``user_id`` field — that always comes from the JWT (see module docstring).
    ``book_ids`` / ``collection_ids`` (Phase 49) are OPTIONAL scope
    narrowers: when present, ``run_search`` intersects them with the JWT
    user's resolved ``user_library`` (``effective = requested & library``) so
    a client can only SHRINK scope, never widen it; when omitted the whole
    library is searched (backward compatible). ``extra="forbid"`` (Phase 18)
    still makes a smuggled ``user_id`` — or any other unknown field — a hard
    422, not a silently-dropped key (closes Phase 12 deviation d).
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1024)
    limit: int = Field(default=10, ge=1, le=100)
    # Phase 49 scope narrowers. The caps are the single 422 owner (the web
    # whitelist does structural-only checks). ``book_ids`` matches the
    # collections add/remove cap (a 10K-book library is bulk-scopable in one
    # call); ``collection_ids`` is the smaller folder-count cap. ``None`` /
    # omitted = whole library; both are INTERSECTED with the JWT library in
    # ``run_search`` so they can only narrow.
    book_ids: list[uuid.UUID] | None = Field(default=None, max_length=10_000)
    collection_ids: list[uuid.UUID] | None = Field(default=None, max_length=500)
    # Phase 13 toggle. Default true so the canonical /search pipeline
    # matches ARCHITECTURE.md §5 (hybrid → rerank → highlight) and so
    # Phase 14's summary endpoint can call /search and get pre-pruned
    # context without an extra flag. Set false to compare against raw
    # RRF (Phase 12 behavior) — useful for debugging ranking shifts.
    rerank: bool = Field(default=True)


class SearchHit(BaseModel):
    book_id: uuid.UUID
    content_chunk: str
    metadata: dict[str, Any]
    score: float


class SearchResponse(BaseModel):
    """``POST /search`` response.

    ``degraded`` (Phase 22) names the pipeline stages that failed and were
    bypassed for this response, in pipeline order — any of ``"dense"``,
    ``"sparse"``, ``"rerank"``, ``"highlight"``. Always present and ``[]``
    when healthy (no optional-omission: a stable, always-there field is
    counter-friendly for the Phase 27 metrics and additive for clients —
    response models don't set ``extra="forbid"``, and web's TS interfaces
    ignore unknown JSON fields until a web phase renders it).
    """

    hits: list[SearchHit]
    degraded: list[str] = Field(default_factory=list)


def _embed_query(query: str) -> list[float]:
    """Embed a single query with BGE-Large (remote). Blocking; offload via ``to_thread``."""
    arr = embed([query])
    return arr[0].tolist()


def _dense_milvus_leg(query_vec: list[float], book_ids: list[uuid.UUID]) -> list[RetrievalHit]:
    """Client checkout + Milvus search — the arm's whole Milvus surface. Sync.

    Runs in a worker thread under the ``DENSE_ARM_BUDGET_SECONDS``
    ``wait_for`` in ``_dense_arm``. Client construction lives INSIDE the
    budget (and off the event loop) on purpose: a rebuild after a reset can
    itself stall — 2.5 s channel-ready wait on a fresh connection, and
    pymilvus's idle health check can even run its hardcoded-10 s reconnect
    inside ``make_client``. On ``MilvusException`` the singleton is reset so
    the next request reconstructs; this also fires from a budget-orphaned
    thread when its in-flight call eventually fails — exactly the case where
    the client is known bad. A construction failure needs no reset: nothing
    was published.
    """
    client = _client()
    try:
        return dense_search(
            client=client,
            query_vec=query_vec,
            book_ids=book_ids,
            limit=DENSE_FANOUT,
        )
    except MilvusException:
        _reset_client(client)
        raise


async def _dense_arm(query: str, book_ids: list[uuid.UUID]) -> list[RetrievalHit]:
    """Embed the query, then run the budget-bounded Milvus leg — off the event loop.

    Only the Milvus leg sits under the ``wait_for`` budget: the remote embed
    has its own ``RemoteInferenceError`` taxonomy and retry budget
    (``worker/inference.py``), and a blanket arm budget would cut those
    legitimate retries short. Budget expiry raises ``TimeoutError`` — an
    ``Exception``, so the gather in ``run_search`` degrades the arm; genuine
    cancellation re-raises ``CancelledError`` through ``wait_for`` and stays
    flow control (pinned in tests).
    """
    # Phase 27: per-stage retrieval timing. ``Histogram.time()`` is a sync
    # context manager that records the wall-clock span on exit — wrapping the
    # ``await`` measures the leg's real latency (embed remote call; the
    # budget-bounded Milvus leg) without changing query shape or control flow.
    with RETRIEVAL_STAGE.labels(stage="embed").time():
        query_vec = await asyncio.to_thread(_embed_query, query)
    with RETRIEVAL_STAGE.labels(stage="dense").time():
        return await asyncio.wait_for(
            asyncio.to_thread(_dense_milvus_leg, query_vec, book_ids),
            timeout=DENSE_ARM_BUDGET_SECONDS,
        )


async def _sparse_arm(
    *,
    session: AsyncSession,
    query: str,
    book_ids: list[uuid.UUID],
) -> list[RetrievalHit]:
    """Time the BM25 (sparse) leg into the retrieval-stage histogram (Phase 27).

    A thin timing wrapper around ``bm25_search`` — same tenant-scoped call
    (``book_id = ANY(book_ids)``), same ``SPARSE_FANOUT`` limit, no query-shape
    change. ``bm25_search`` is referenced via the module global so the existing
    ``run_search`` degradation tests (which monkeypatch ``search.bm25_search``)
    keep working unchanged.
    """
    with RETRIEVAL_STAGE.labels(stage="sparse").time():
        return await bm25_search(
            session=session,
            query=query,
            book_ids=book_ids,
            limit=SPARSE_FANOUT,
        )


def _to_search_hit(hit: RetrievalHit) -> SearchHit:
    return SearchHit(
        book_id=hit.book_id,
        content_chunk=hit.content_chunk,
        metadata=hit.metadata,
        score=hit.score,
    )


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """``run_search``'s result: the final hits plus the degraded-stage flags.

    ``degraded`` lists the pipeline stages that failed and were bypassed
    (``"dense"`` / ``"sparse"`` / ``"rerank"`` / ``"highlight"``, pipeline
    order); ``[]`` means the full pipeline ran. Both HTTP callers copy it
    verbatim onto their response models.
    """

    hits: list[SearchHit]
    degraded: list[str]


# Fixed 503 detail for the both-arms-down case. Per-arm exceptions are
# already in the log (exc_info); the body never carries failure detail —
# connection errors can embed hosts and DSNs (the Phase 18 /readyz rule).
_RETRIEVAL_UNAVAILABLE_DETAIL = "Search is temporarily unavailable; please retry shortly."

# Phase 49: a no-oracle 404 for a scope ``collection_id`` the JWT user does not
# own (foreign, nonexistent, or already-deleted). Byte-identical to the
# ``collections_routes`` posture — a search scope must never become an
# existence oracle for another tenant's collection.
_COLLECTION_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="Collection not found.",
)


def _surviving_arm_hits(
    arm: str,
    result: list[RetrievalHit] | BaseException,
    degraded: list[str],
) -> list[RetrievalHit]:
    """Unpack one ``return_exceptions=True`` gather arm (Phase 22).

    Hits pass through; an ``Exception`` degrades — logged loudly with the
    traceback, named in *degraded*, and replaced by an empty arm so RRF
    fusion reduces to the surviving arm's ranking. No ``except`` clause is
    involved (the gather hands us exception *objects*), and the breadth is
    deliberate: the dense arm's failure surface spans four libraries
    (pymilvus ``MilvusException``, ``RemoteInferenceError`` from the remote
    embed, ``psycopg`` errors + ``RuntimeError`` from the embedding-space
    guard) and the sparse arm raises SQLAlchemy/asyncpg DBAPI errors —
    enumerating them would couple this module to transport internals.
    Non-``Exception`` ``BaseException``s (``CancelledError``) are flow
    control, not dependency failures, and are re-raised.
    """
    if isinstance(result, BaseException):
        if not isinstance(result, Exception):
            raise result
        logger.warning(
            "retrieval %s arm failed; degrading to the surviving arm",
            arm,
            exc_info=result,
        )
        degraded.append(arm)
        # Phase 27: a non-zero counter under healthy deps is the in-our-code-bug
        # tell (the Phase 22 trust-gap mitigation; api/AGENTS.md "Open trust
        # gaps"). Incremented at the SAME site that appends to ``degraded``.
        RETRIEVAL_DEGRADED.labels(stage=arm).inc()
        return []
    return result


async def run_search(
    *,
    query: str,
    limit: int,
    do_rerank: bool,
    user_id: uuid.UUID,
    session: AsyncSession,
    requested_book_ids: list[uuid.UUID] | None = None,
    requested_collection_ids: list[uuid.UUID] | None = None,
) -> SearchOutcome:
    """Hybrid → (rerank → highlight) retrieval over *user_id*'s library, minus HTTP.

    The reusable core of ``POST /search``. Phase 14's ``/search-summary``
    (``api/summary.py``) calls this with ``do_rerank=True`` to feed Gemini
    the same reranked + sentence-pruned context the canonical pipeline
    produces (ARCHITECTURE.md §5) — without re-implementing rerank/highlight
    or round-tripping through the HTTP handler.

    Tenant scoping is identical to the handler and just as load-bearing:
    *user_id* MUST be the JWT-derived ``current_user.user_id`` (never a
    client-supplied value), and the ``book_id`` set is resolved server-side
    from ``user_library`` for that user. An empty library short-circuits to
    an empty outcome before any embedding or remote call.

    Phase 49 — optional client scope: *requested_book_ids* /
    *requested_collection_ids* (from the request body) NARROW the search.
    The resolved library is the UNIVERSE; the effective set is always
    ``requested & library`` (after ownership-checking the collections and
    unioning their member books), so a client can only SHRINK scope, never
    widen it — ``effective ⊆ library`` is the non-negotiable invariant. A
    foreign/nonexistent ``collection_id`` is a no-oracle 404; both fields
    ``None``/omitted searches the whole library. An empty effective set
    (disjoint request, or empty intersection) short-circuits to empty
    BEFORE any embed/remote call, exactly like an empty library.

    Degradation (Phase 22) NEVER widens that scope: ``book_ids`` is resolved
    exactly once above the fan-out and the same list parameterizes both arms
    (Milvus ``book_id in [...]``, SQL ``book_id = ANY(...)``) — a surviving
    arm's hits already cleared the tenant filter, and every fallback below is
    a pure reshuffle/truncation of in-memory hits (no re-query, no recomputed
    filter; both arms still raise on an empty ``book_id`` set rather than
    search unfiltered). Both arms down → 503 (see the module docstring for
    the status-code rationale).
    """
    stmt = select(UserLibraryEntry.book_id).where(
        UserLibraryEntry.user_id == user_id,
    )
    library: set[uuid.UUID] = set((await session.execute(stmt)).scalars().all())
    if not library:
        return SearchOutcome(hits=[], degraded=[])

    # Phase 49: optional client-supplied scope. The library resolved above is
    # the UNIVERSE; a request can only narrow within it. Omitting both fields
    # searches the whole library (backward compatible).
    if requested_book_ids is None and requested_collection_ids is None:
        effective = library
    else:
        requested: set[uuid.UUID] = set(requested_book_ids or [])
        if requested_collection_ids is not None:
            # Ownership-check the collection ids against the JWT user FIRST: a
            # foreign/nonexistent id is a no-oracle 404 (the Phase 48
            # _require_owned_collection posture, in set form), never an oracle
            # confirming another tenant's collection exists. Both stmt builders
            # also carry the ``user_id`` predicate (defense in depth).
            owned = set(
                (
                    await session.execute(
                        _owned_collection_ids_stmt(requested_collection_ids, user_id),
                    )
                )
                .scalars()
                .all(),
            )
            if owned != set(requested_collection_ids):
                raise _COLLECTION_NOT_FOUND
            # Union in the member books of the (now all-owned) collections.
            members = (
                await session.execute(
                    _member_book_ids_stmt(requested_collection_ids, user_id),
                )
            ).scalars().all()
            requested |= set(members)
        # THE INVARIANT (CLAUDE.md): intersect with the library — the effective
        # set can only SHRINK, never widen. ``effective ⊆ library`` always.
        effective = requested & library

    if not effective:
        # Short-circuit BEFORE any embed/remote call: an empty effective set
        # (disjoint request, or an empty intersection) can return nothing, so
        # we never run the model or issue a ``book_id in []`` filter.
        return SearchOutcome(hits=[], degraded=[])

    # Sorted so both retrieval arms receive a deterministic, identical book set.
    book_ids: list[uuid.UUID] = sorted(effective)

    # Phase 22: return_exceptions=True so one arm's failure degrades to the
    # surviving arm instead of bubbling up as a 500 (the Phase 12 audit's
    # one-arm-down failure mode).
    dense_result, sparse_result = await asyncio.gather(
        _dense_arm(query, book_ids),
        _sparse_arm(session=session, query=query, book_ids=book_ids),
        return_exceptions=True,
    )
    degraded: list[str] = []
    dense_hits = _surviving_arm_hits("dense", dense_result, degraded)
    sparse_hits = _surviving_arm_hits("sparse", sparse_result, degraded)
    # Order-insensitive on purpose: a list compare would silently couple
    # both-down detection to the unpack order above.
    if set(degraded) == {"dense", "sparse"}:
        # Both arms down: there is nothing to degrade TO. 503, not 500 — a
        # dependency outage is a known, retryable service state (the /readyz
        # and unconfigured-key precedents), not a bug deserving a stack; and
        # not 502 — Milvus/Postgres are internal infra, not an upstream
        # gateway. Both tracebacks are already logged above.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_RETRIEVAL_UNAVAILABLE_DETAIL,
        )

    # Fan-out depends on whether the post-retrieval stages run: keep 30
    # so the cross-encoder has the full recall pool to reorder; cap at
    # `limit` when skipping rerank so we don't ship more rows than the
    # caller asked for.
    fused_limit = RERANK_FANOUT if do_rerank else limit
    fused = rrf_fuse(dense=dense_hits, sparse=sparse_hits, limit=fused_limit)

    if not do_rerank:
        return SearchOutcome(hits=[_to_search_hit(h) for h in fused], degraded=degraded)

    # Phase 22: rerank + highlight each degrade independently to the raw RRF
    # ordering instead of 500ing. RemoteInferenceError is the entire realistic
    # failure surface (both stages are pure remote calls since Phase 16b, and
    # it covers MissingInferenceKeyError); anything else is a pipeline bug
    # that must fail loud. Falling back to RRF scores can't break a consumer:
    # nothing thresholds the rerank score (ADR 0006) — ``score`` simply
    # carries RRF semantics, same as ``rerank=false``.
    try:
        with RETRIEVAL_STAGE.labels(stage="rerank").time():
            ranked = await asyncio.to_thread(
                rerank,
                query=query,
                hits=fused,
                top_n=limit,
            )
    except RemoteInferenceError:
        logger.warning("rerank failed; falling back to raw RRF top-%d", limit, exc_info=True)
        degraded.append("rerank")
        RETRIEVAL_DEGRADED.labels(stage="rerank").inc()
        ranked = list(fused[:limit])

    # A rerank failure does NOT skip highlight: highlight needs only the
    # query and a hit list, so it prunes the RRF-ordered fallback just as
    # well. If the rerank failure was a provider outage, this call simply
    # degrades too (same provider) and both flags appear.
    try:
        with RETRIEVAL_STAGE.labels(stage="highlight").time():
            pruned = await asyncio.to_thread(
                highlight,
                query=query,
                hits=ranked,
            )
    except RemoteInferenceError:
        logger.warning("highlight failed; returning unpruned hits", exc_info=True)
        degraded.append("highlight")
        RETRIEVAL_DEGRADED.labels(stage="highlight").inc()
        pruned = ranked

    return SearchOutcome(hits=[_to_search_hit(h) for h in pruned], degraded=degraded)


@router.post("", response_model=SearchResponse)
async def search(
    payload: SearchRequest,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> SearchResponse:
    """Top-K hybrid search over the authenticated user's library.

    Thin HTTP wrapper over ``run_search`` — see that function for the
    pipeline and the tenant-scoping contract. When ``payload.rerank`` is
    true (default), the top-30 fused list flows through the remote
    reranker (→ top ``limit``) and BGE-M3 sentence-level pruning (drops
    sentences below threshold 0.5). When false, the raw RRF top-K is
    returned (Phase 12 behavior). ``degraded`` names any pipeline stage
    that failed and was bypassed (Phase 22; module docstring).
    """
    outcome = await run_search(
        query=payload.query,
        limit=payload.limit,
        do_rerank=payload.rerank,
        user_id=current_user.user_id,
        session=session,
        requested_book_ids=payload.book_ids,
        requested_collection_ids=payload.collection_ids,
    )
    return SearchResponse(hits=outcome.hits, degraded=outcome.degraded)
