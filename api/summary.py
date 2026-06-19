"""Gemini Flash summary agent — grounded 1–2 paragraph synthesis with citations.

Phase 14 (ARCHITECTURE.md §1 goal, §2 "LLM" row, §5 lifecycle final step);
transport re-cut to the openai SDK over OpenAI-compatible endpoints in
Phase 14b (ADR 0005). ``POST /search-summary`` is the user-facing endpoint
the whole platform builds toward: a natural-language question in, a short
grounded answer with citations back to the user's own books out.

## Pipeline reuse

This module does **not** re-implement retrieval. It calls
``search.run_search(do_rerank=True)`` — the exact Phase 12→13 path the
canonical ``POST /search`` uses (hybrid dense+sparse → RRF → cross-encoder
rerank → BGE-M3 sentence pruning). What comes back is already the
precision-ranked, token-pruned context; this module's only new work is
turning those chunks into a citation-marked prompt, calling the LLM, and
mapping the model's inline markers back to chunk provenance.

## Citation contract

Each retrieved+pruned chunk becomes a *source* with a marker
``[<book>:<chunk_index>]``:

- ``<book>`` is the book's ``global_books.title`` with ``[``, ``]`` and
  ``:`` stripped + whitespace collapsed (so the marker stays
  machine-parseable), falling back to the filename stem and then a short
  ``book-<id8>`` slug. Two books that reduce to the same label are
  de-collided with a ``(2)``/``(3)`` suffix.
- ``<chunk_index>`` is unique per book (``uq_chunks_book_chunk``), so every
  ``(book_id, chunk_index)`` maps to exactly one marker.

The model is instructed to cite those exact markers inline. The returned
``citations`` list is the subset of sources whose markers actually appear in
the summary text, in order of first appearance — so a client can resolve
every marker, and a marker the model paraphrases or invents simply doesn't
resolve (it is never returned as a fake citation). The model also tends to
collapse adjacent citations into one comma-merged bracket like
``[Faith:7, Hope:9]``; each member that resolves against the source set is
returned, so a merged group is not silently dropped. See
``_extract_citations``.

## Grounding & the hallucination guard

Two layers keep the answer grounded (Phase 14 verify: "query nothing-in-corpus
→ response says so, doesn't confabulate"):

1. **Deterministic** — if retrieval returns nothing (empty library, or every
   chunk pruned below the highlight threshold for an off-corpus query, e.g.
   the Phase 12/13 "Theodore Roosevelt" case), we return a fixed message with
   no citations and never call the LLM.
2. **Instructed** — for non-empty-but-weak context, the system instruction
   tells the model to use only the provided passages and to say so plainly
   when they don't address the question.

## Config & failure modes

- ``SERMON_API_LLM_PROVIDER`` picks the OpenAI-compatible endpoint —
  ``deepinfra`` (default since 2026-06-12, amending ADR 0005's original
  ``google`` default; DeepInfra's compat endpoint, ``DEEPINFRA_API_KEY`` —
  the same key the embeddings/rerank/highlight legs already use),
  ``google`` (Google's compat endpoint, ``GOOGLE_API_KEY``), or ``ppq``
  (ppq.ai gateway, ``PPQ_API_KEY``). ``_PROVIDERS`` below is the
  single source of truth for base_url / default model / key env var, and
  ``SERMON_API_LLM_MODEL`` overrides the model id (ADR 0005).
- The **active** provider's key is required. The handler 503s up front —
  naming the missing env var — when it's unset rather than burning ~30s of
  CPU rerank just to fail at the LLM call.
- The OpenAI client is lazy + ``@lru_cache``'d, mirroring the model loaders
  in ``rerank.py`` / ``highlight.py`` — import / lint / test never need a key
  or network.
- An upstream API error or an empty completion becomes a 502. Other
  unexpected failures fall through to a 500 — fail loud on bugs; dependency
  blips degrade instead (next section).

## Degraded retrieval: proceed with the flag, never a 503 (Phase 22)

``run_search`` degrades gracefully when a retrieval arm / rerank /
highlight fails (``search.py`` module docstring) and reports the bypassed
stages on ``SearchOutcome.degraded``. This endpoint's posture — decided in
Phase 22 — is **proceed-with-flag**: a degraded-retrieval summary still
runs over the surviving arm's context and the response's ``degraded`` list
carries the flags so clients can caveat the answer. Rationale: the
surviving arm's hits passed the exact same JWT-derived ``user_library``
filter as the happy path (degradation never widens scope), so the result
is *narrower* grounding, not *wrong* grounding — the citation contract
holds unchanged (every returned citation resolves to a chunk the user is
authorized to read), and a partial answer with a caveat beats refusing the
user's question outright. A 503 here would also be dishonest about
recoverability: the summary leg itself is healthy. Both retrieval arms
down is the exception — ``run_search`` raises a real 503 (there is nothing
to summarize over). A degraded-EMPTY retrieval keeps the deterministic
no-context guard (no LLM call) but still carries the flags, so the client
can distinguish "your library has nothing on this" from "search was
partially down, the empty result may reflect the outage".

## Tenant surface

None new. ``run_search`` owns the load-bearing tenant filter (JWT
``user_id`` → ``user_library`` book set). The title lookup is keyed only by
book_ids that already cleared that filter, and ``global_books`` is
shared-by-design (ARCHITECTURE.md §3/§4 dedup) — a title is not user-scoped
data. The LLM sees only chunk text the user is already authorized to read.

## Latency

Warm reranked retrieval is ~30s on CPU (``api/AGENTS.md`` open gap) before
the LLM is even called, so the ``/search-summary`` E2E is that plus the LLM
round-trip — well over the ARCHITECTURE.md §1 ``< 1s`` target. Acceptable for
sermon prep; the architecture-locked GPU swap is the documented path.
"""

# The openai SDK ships py.typed with fully-typed chat completions, so this
# module needs no stub relaxation like the pymilvus-touching modules do.

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache

import openai
from db import GlobalBook
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import ratelimit
from auth import CurrentUserDep, SessionDep
from metrics import RETRIEVAL_STAGE
from search import SearchHit, run_search
from settings import settings

router = APIRouter(prefix="/search-summary", tags=["summary"])


@dataclass(frozen=True, slots=True)
class _Provider:
    """One OpenAI-compatible endpoint the summary agent can talk to (ADR 0005)."""

    base_url: str
    default_model: str
    # Named in the 503/RuntimeError detail so the operator knows what to set.
    key_env_var: str
    # Late-bound so the per-request guard (and tests monkeypatching settings)
    # always sees the current value, never an import-time snapshot.
    api_key: Callable[[], str | None]
    # Per-provider ``reasoning_effort`` applied when SERMON_API_LLM_REASONING_EFFORT
    # is unset (the env knob always wins when set). ``None`` → the param is
    # omitted from the request entirely — the safe stance for endpoints whose
    # tolerance of it is unprobed (ppq's chat.completions, ADR 0006) or whose
    # default behavior is already acceptable (google separates thinking from
    # the returned text). Only set a value here that the provider is
    # live-verified to honor.
    default_reasoning_effort: str | None = None


# Single source of truth for provider → endpoint/model/key, selected via
# SERMON_API_LLM_PROVIDER (settings.llm_provider). Model ids are pinned, not
# alias-tracking (ppq's ~"gemini-flash-latest" drifts) — a silent model swap
# under the pinned citation contract is exactly the failure mode the Phase 14b
# live verify exists to catch. Note the spelling differs per provider: bare
# id on google, "google/"-prefixed on ppq; an SERMON_API_LLM_MODEL override
# must use the active provider's spelling.
_PROVIDERS: Mapping[str, _Provider] = {
    "google": _Provider(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        default_model="gemini-2.5-flash",
        key_env_var="GOOGLE_API_KEY",
        api_key=lambda: settings.google_api_key,
    ),
    "ppq": _Provider(
        base_url="https://api.ppq.ai/v1",
        default_model="google/gemini-2.5-flash",
        key_env_var="PPQ_API_KEY",
        api_key=lambda: settings.ppq_api_key,
    ),
    # Phase 16b (ADR 0006): DeepInfra serves google/gemini-2.5-flash over its
    # OpenAI-compatible chat endpoint (the same base_url the embeddings leg
    # uses), keyed by the same DEEPINFRA_API_KEY — so the whole inference stack
    # collapses to one vendor + one key. reasoning_effort=none is honored here
    # (probed live 2026-06-09), unlike ppq's chat.completions.
    #
    # default_reasoning_effort="none": without it, DeepInfra-served Gemini 2.5
    # Flash runs thinking by default AND inlines the literal <think>...</think>
    # block into message.content (defect found in the 2026-06-12 live verify of
    # the deepinfra-default flip) — the raw reasoning would land in the user's
    # summary. "none" is live-verified honored on this endpoint, so the default
    # experience is think-free with no operator env edits; ppq/google rows keep
    # omitting the param (unprobed on ppq's chat.completions / not needed on
    # google, whose compat layer keeps thinking out of the returned text).
    "deepinfra": _Provider(
        base_url="https://api.deepinfra.com/v1/openai",
        default_model="google/gemini-2.5-flash",
        key_env_var="DEEPINFRA_API_KEY",
        api_key=lambda: settings.deepinfra_api_key,
        default_reasoning_effort="none",
    ),
}


def _active_provider() -> _Provider:
    """The row picked by ``SERMON_API_LLM_PROVIDER``.

    ``settings.llm_provider`` is ``Literal["google", "ppq", "deepinfra"]``, so
    the lookup cannot ``KeyError`` on a validated config.
    """
    return _PROVIDERS[settings.llm_provider]


# Low temperature for grounded, low-confabulation synthesis; 1–2 paragraphs
# with inline markers fit comfortably under the output cap.
_TEMPERATURE = 0.2
_MAX_OUTPUT_TOKENS = 768

# Cap on a citation label's length so a pathologically long title doesn't
# bloat every marker in the prompt.
_MAX_LABEL_LEN = 80

_NO_CONTEXT_MESSAGE = "I couldn't find anything in your library that addresses that question."

_SYSTEM_INSTRUCTION = (
    "You are a careful research assistant for a personal theological library. "
    "Using ONLY the provided context passages, write a thematic summary of one "
    "to two paragraphs that answers the user's question. Cite your sources "
    "inline using their exact bracketed [book:chunk] markers, placed "
    "immediately after the statement each one supports. Do not rely on any "
    "knowledge outside the provided passages, and do not invent citations. If "
    "the passages do not address the question, say so in a single sentence "
    "instead of guessing."
)

# Characters that would break the `[book:chunk]` marker structure if they
# survived into the book label.
_LABEL_BANNED = re.compile(r"[\[\]:]+")
_WHITESPACE = re.compile(r"\s+")


class SummaryRequest(BaseModel):
    """Summary payload.

    No ``user_id`` / ``book_ids`` fields — tenant scope is resolved
    server-side by ``run_search`` from the JWT (see ``search.py``).
    ``extra="forbid"`` (Phase 18) makes a smuggled extra field a hard
    422 instead of a silently-dropped key.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1024)
    # Feeds ``run_search``'s ``limit`` (the cross-encoder's top-N). The rerank
    # fan-out is 30, so values above that just return the full reranked pool.
    limit_chunks: int = Field(default=20, ge=1, le=100)


class Citation(BaseModel):
    """One resolvable source behind the summary — maps a marker to its chunk.

    ``content`` is the pruned chunk text that grounded the summary (Phase 16:
    the citation cards render it as a preview). It is exactly the passage the
    LLM saw — already tenant-filtered by ``run_search`` — so returning it adds
    no new tenant surface.
    """

    marker: str
    book_id: uuid.UUID
    title: str
    chunk_index: int
    content: str
    filename: str | None = None
    parent_section: str | None = None


class SummaryResponse(BaseModel):
    """``POST /search-summary`` response.

    ``degraded`` (Phase 22) is copied verbatim from ``run_search``'s outcome
    — same stage names and always-present-``[]``-when-healthy convention as
    ``search.SearchResponse.degraded`` (see the module docstring for why a
    degraded summary proceeds with the flag instead of 503ing).
    """

    summary: str
    citations: list[Citation]
    degraded: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Source:
    """A retrieved chunk plus the citation marker assigned to it."""

    marker: str
    book_id: uuid.UUID
    title: str
    chunk_index: int
    content: str
    filename: str | None
    parent_section: str | None


@lru_cache(maxsize=1)
def _client() -> openai.OpenAI:
    """Construct the active provider's chat-completions client once per process.

    Lazy + cached so import / lint / tests never need a key or network — only
    the first ``/search-summary`` that actually reaches the LLM pays the
    construction. The handler validates the key up front and 503s when unset,
    so this never runs unconfigured in practice; the guard keeps that
    invariant explicit (``lru_cache`` does not cache the raised exception, so
    setting the key later still works).

    ``SERMON_API_LLM_BASE_URL`` (``settings.llm_base_url``) overrides the active
    provider row's hardcoded ``base_url`` — the sole intended use is pointing at
    a local, deterministic OpenAI-compatible stub for the web Phase 25 E2E/CI so
    no real provider round-trip fires (settings.py documents the non-prod
    posture). The active provider's key is still required (a dummy value
    satisfies the up-front 503 guard; the stub ignores it).
    """
    provider = _active_provider()
    api_key = provider.api_key()
    if not api_key:
        msg = f"{provider.key_env_var} is not configured"
        raise RuntimeError(msg)
    base_url = settings.llm_base_url or provider.base_url
    return openai.OpenAI(base_url=base_url, api_key=api_key)


def _label_from_title(title: str, filename: str | None, book_id: uuid.UUID) -> str:
    """Reduce a title to a marker-safe label; fall back to filename stem, then id."""
    cleaned = _WHITESPACE.sub(" ", _LABEL_BANNED.sub("", title)).strip()
    if cleaned:
        return cleaned[:_MAX_LABEL_LEN].strip()
    if filename:
        stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        stem = _WHITESPACE.sub(" ", _LABEL_BANNED.sub("", stem)).strip()
        if stem:
            return stem[:_MAX_LABEL_LEN].strip()
    return f"book-{book_id!s:.8}"


def _assign_book_labels(
    hits: Sequence[SearchHit],
    titles: Mapping[uuid.UUID, str],
) -> dict[uuid.UUID, str]:
    """Map each distinct book_id to a unique, parseable citation label.

    De-collides labels that reduce to the same string with a ``(2)``/``(3)``
    suffix so every ``(book_id, chunk_index)`` resolves to exactly one marker.
    """
    labels: dict[uuid.UUID, str] = {}
    used: set[str] = set()
    for hit in hits:
        if hit.book_id in labels:
            continue
        filename = hit.metadata.get("filename")
        base = _label_from_title(
            titles.get(hit.book_id) or "",
            filename if isinstance(filename, str) else None,
            hit.book_id,
        )
        label = base
        suffix = 2
        while label in used:
            label = f"{base} ({suffix})"
            suffix += 1
        labels[hit.book_id] = label
        used.add(label)
    return labels


def _build_sources(
    hits: Sequence[SearchHit],
    titles: Mapping[uuid.UUID, str],
) -> list[_Source]:
    """Assign a marker to each hit, preserving the reranked (most-relevant-first) order.

    ``chunk_index`` is read straight from the hit metadata — both retrieval
    arms write it (``worker/retrieval.py``) and rerank/highlight preserve it.
    Its absence is a pipeline bug we let surface (KeyError → 500) rather than
    silently coerce, matching ``dense_search``'s posture.
    """
    labels = _assign_book_labels(hits, titles)
    sources: list[_Source] = []
    for hit in hits:
        chunk_index = int(hit.metadata["chunk_index"])
        filename = hit.metadata.get("filename")
        parent_section = hit.metadata.get("parent_section")
        label = labels[hit.book_id]
        sources.append(
            _Source(
                marker=f"[{label}:{chunk_index}]",
                book_id=hit.book_id,
                title=titles.get(hit.book_id) or label,
                chunk_index=chunk_index,
                content=hit.content_chunk,
                filename=filename if isinstance(filename, str) else None,
                parent_section=(parent_section if isinstance(parent_section, str) else None),
            ),
        )
    return sources


def _build_prompt(query: str, sources: Sequence[_Source]) -> str:
    """Assemble the user-turn content: the question + marker-prefixed passages."""
    context = "\n\n".join(f"{s.marker}\n{s.content}" for s in sources)
    return (
        f"Question: {query}\n\n"
        "Context passages (each begins with its [book:chunk] citation marker):\n\n"
        f"{context}"
    )


def _generate_summary(*, query: str, sources: Sequence[_Source]) -> str:
    """Call the configured LLM with the grounded prompt. Blocking; offload via ``to_thread``.

    Raises ``HTTPException`` (502) on an upstream API error or an empty
    completion — FastAPI re-raises it from the worker thread into the handler.
    """
    provider = _active_provider()
    # Phase 16b: optionally disable/cap thinking (SERMON_API_LLM_REASONING_EFFORT).
    # Sent via extra_body so the knob is provider-agnostic and forward-compatible
    # with values the SDK's typed literal hasn't caught up to (e.g. "none").
    # When the env knob is unset, the active provider row's
    # default_reasoning_effort applies (deepinfra → "none", else omitted) —
    # see the _PROVIDERS deepinfra comment for the <think>-inlining defect
    # this guards against.
    reasoning: str | None = settings.llm_reasoning_effort
    if reasoning is None:
        reasoning = provider.default_reasoning_effort
    extra_body = {"reasoning_effort": reasoning} if reasoning is not None else None
    try:
        response = _client().chat.completions.create(
            model=settings.llm_model or provider.default_model,
            messages=[
                {"role": "system", "content": _SYSTEM_INSTRUCTION},
                {"role": "user", "content": _build_prompt(query, sources)},
            ],
            temperature=_TEMPERATURE,
            max_tokens=_MAX_OUTPUT_TOKENS,
            extra_body=extra_body,
        )
    except openai.APIError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Summary generation failed upstream.",
        ) from exc
    text = response.choices[0].message.content if response.choices else None
    if text is None or not text.strip():
        # Empty completion (e.g. a safety filter blocked the response, or the
        # gateway returned no choices). Fail loud rather than fabricate a
        # summary.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Summary generation returned no content.",
        )
    return text.strip()


def _to_citation(source: _Source) -> Citation:
    return Citation(
        marker=source.marker,
        book_id=source.book_id,
        title=source.title,
        chunk_index=source.chunk_index,
        content=source.content,
        filename=source.filename,
        parent_section=source.parent_section,
    )


# Pulls each bracket group out of the summary — the inner text never contains a
# nested bracket (labels strip ``[``/``]``, see ``_LABEL_BANNED``), so a flat
# ``[^\[\]]*`` body is exact. Used to walk merged groups like ``[A:7, B:9]``.
_BRACKET_GROUP = re.compile(r"\[[^\[\]]*\]")


def _resolve_group_members(inner: str, by_inner: Mapping[str, _Source]) -> list[_Source]:
    """Resolve the comma-separated members of one bracket body to known sources.

    *inner* is the text between a ``[`` and ``]`` (no nested brackets). Each
    member is a ``<label>:<chunk_index>`` marker; the LLM may merge several into
    one group as ``[A:7, B:9]``. Resolution is greedy longest-prefix against the
    known inner-marker set rather than a naive ``split(",")`` because a book
    *label* can itself contain commas (only ``[``/``]``/``:`` are stripped, see
    ``_label_from_title``), so ``[Faith, Hope:7]`` is a single member, not two.

    At each separator boundary we take the longest known marker the remaining
    text starts with that is then followed by end-of-group or a comma — longest
    wins so a label-internal comma is absorbed before we split on it. Text that
    resolves to no known marker is skipped to the next comma and never returned,
    so an invented member can't fabricate a citation.
    """
    resolved: list[_Source] = []
    pos = 0
    n = len(inner)
    while pos < n:
        # Skip leading whitespace before a member.
        while pos < n and inner[pos] == " ":
            pos += 1
        if pos == n:
            break
        best: _Source | None = None
        best_end = pos
        for candidate, source in by_inner.items():
            end = pos + len(candidate)
            # The candidate must match here, end past the current best (longest
            # wins, absorbing a label-internal comma), AND be bounded by
            # end-of-group or a comma — else ``Faith:1`` matches in ``Faith:12``.
            if end <= best_end or not inner.startswith(candidate, pos):
                continue
            if end == n or inner[end] == ",":
                best = source
                best_end = end
        if best is not None:
            resolved.append(best)
            pos = best_end
            # Step over the separating comma (and any following space handled at
            # the top of the loop).
            if pos < n and inner[pos] == ",":
                pos += 1
        else:
            # Unresolvable member: skip to the next comma so a stray token can't
            # derail the rest of the group, and never emit it as a citation.
            nxt = inner.find(",", pos)
            if nxt == -1:
                break
            pos = nxt + 1
    return resolved


def _extract_citations(summary_text: str, sources: Sequence[_Source]) -> list[Citation]:
    """Return the sources whose markers appear in *summary_text*, first-appearance order.

    Only markers we handed the model resolve — a marker the model paraphrases
    or invents is ignored, so the citation list never carries an unresolvable
    or hallucinated reference. Markers are bracket-delimited, so no marker is a
    substring of another (``[X:1]`` cannot match inside ``[X:12]``).

    Comma-merged brackets are handled member-by-member: the model often collapses
    adjacent citations into one group like ``[Faith:7, Hope:9]``, and every
    member that resolves against the source set is returned (the v0 silent-drop
    of merged-only members is gone). A single-marker bracket behaves exactly as
    before. Resolution is greedy longest-prefix against the known marker set,
    not a naive comma split, because a book label can itself contain a comma
    (only ``[``/``]``/``:`` are stripped), so ``[Faith, Hope:7]`` is one member.

    First-appearance order is by the bracket group's position in *summary_text*;
    within a merged group, by member order. The result is de-duped by marker,
    first occurrence winning.
    """
    by_inner: dict[str, _Source] = {}
    for source in sources:
        # Strip the surrounding ``[``/``]`` to get the comparable member text.
        inner = source.marker[1:-1]
        by_inner.setdefault(inner, source)

    seen: set[str] = set()
    citations: list[Citation] = []
    for match in _BRACKET_GROUP.finditer(summary_text):
        body = match.group()[1:-1]
        for source in _resolve_group_members(body, by_inner):
            if source.marker in seen:
                continue
            seen.add(source.marker)
            citations.append(_to_citation(source))
    return citations


async def _resolve_titles(
    session: AsyncSession,
    book_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, str]:
    """Look up ``global_books.title`` for *book_ids*.

    Tenant note: *book_ids* come from ``run_search`` hits already filtered to
    the JWT user's ``user_library`` set, so this adds no new tenant surface;
    ``global_books`` is shared-by-design (ARCHITECTURE.md §3/§4 dedup).
    """
    unique = list(dict.fromkeys(book_ids))
    if not unique:
        return {}
    stmt = select(GlobalBook.book_id, GlobalBook.title).where(
        GlobalBook.book_id.in_(unique),
    )
    rows = (await session.execute(stmt)).tuples().all()
    return {book_id: title for book_id, title in rows}


async def _summary_rate_limit(current_user: CurrentUserDep) -> None:
    """Per-USER limiter (Phase 19) — runs BEFORE the expensive pipeline.

    Keyed on the JWT-derived ``user_id``, never the IP: behind the prod web
    proxy every browser shares one source address, so per-IP keying would
    let a single user exhaust everyone (and never brake that user). Wired
    as a route-decorator dependency so a 429 fires before retrieval or the
    paid LLM call burns anything; FastAPI's per-request dependency cache
    means ``get_current_user`` still runs exactly once. Defined here (not
    in ratelimit.py) so ratelimit never imports auth — no import cycle.
    """
    await ratelimit.enforce("summary_user", str(current_user.user_id))


@router.post("", response_model=SummaryResponse, dependencies=[Depends(_summary_rate_limit)])
async def search_summary(
    payload: SummaryRequest,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> SummaryResponse:
    """Grounded 1–2 paragraph summary over the authenticated user's library.

    Runs the canonical Phase 12→13 retrieval pipeline via
    ``run_search(do_rerank=True)``, builds a citation-marked context from the
    pruned chunks, and asks the configured Gemini Flash model (ADR 0005
    transport) to synthesize a grounded answer (ARCHITECTURE.md §5 final
    step). See the module docstring for the citation contract, the
    hallucination guard, and the tenant surface (inherited wholesale from
    ``run_search``).
    """
    provider = _active_provider()
    if not provider.api_key():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Summary service is not configured; set {provider.key_env_var}.",
        )

    outcome = await run_search(
        query=payload.query,
        limit=payload.limit_chunks,
        do_rerank=True,
        user_id=current_user.user_id,
        session=session,
    )
    hits = outcome.hits
    if not hits:
        # Deterministic half of the hallucination guard: nothing retrieved →
        # say so, never call the LLM. Phase 22: a degraded-empty retrieval
        # short-circuits the same way (nothing to ground on) but keeps the
        # flags so the client can caveat that the emptiness may reflect the
        # outage, not the corpus.
        return SummaryResponse(
            summary=_NO_CONTEXT_MESSAGE,
            citations=[],
            degraded=outcome.degraded,
        )

    titles = await _resolve_titles(session, [h.book_id for h in hits])
    sources = _build_sources(hits, titles)

    # Phase 27: time the LLM leg (the 4th sequential inference leg per summary)
    # into the retrieval-stage histogram as ``stage="llm"``. The ``degraded``
    # list is copied verbatim from ``run_search`` and its stages are already
    # counted in ``search.py`` — do NOT double-count here.
    with RETRIEVAL_STAGE.labels(stage="llm").time():
        summary_text = await asyncio.to_thread(
            _generate_summary,
            query=payload.query,
            sources=sources,
        )
    citations = _extract_citations(summary_text, sources)
    # Proceed-with-flag (Phase 22, module docstring): a degraded retrieval
    # still produced tenant-scoped context, so summarize it and let the
    # flags ride along rather than 503ing a recoverable partial answer.
    return SummaryResponse(
        summary=summary_text,
        citations=citations,
        degraded=outcome.degraded,
    )
