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
resolve (it is never returned as a fake citation). See ``_extract_citations``.

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
  ``google`` (default; Google's compat endpoint, ``GOOGLE_API_KEY``) or
  ``ppq`` (ppq.ai gateway, ``PPQ_API_KEY``). ``_PROVIDERS`` below is the
  single source of truth for base_url / default model / key env var, and
  ``SERMON_API_LLM_MODEL`` overrides the model id (ADR 0005).
- The **active** provider's key is required. The handler 503s up front —
  naming the missing env var — when it's unset rather than burning ~30s of
  CPU rerank just to fail at the LLM call.
- The OpenAI client is lazy + ``@lru_cache``'d, mirroring the model loaders
  in ``rerank.py`` / ``highlight.py`` — import / lint / test never need a key
  or network.
- An upstream API error or an empty completion becomes a 502. Other
  unexpected failures fall through to a 500, the same fail-loud posture the
  retrieval arms take (``search.py`` module docstring; ``api/AGENTS.md``
  open gaps).

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
# module needs no stub relaxation like the pymilvus / sentence-transformers
# modules do.

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache

import openai
from db import GlobalBook
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import CurrentUserDep, SessionDep
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
}


def _active_provider() -> _Provider:
    """The row picked by ``SERMON_API_LLM_PROVIDER``.

    ``settings.llm_provider`` is ``Literal["google", "ppq"]``, so the lookup
    cannot ``KeyError`` on a validated config.
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
    """

    query: str = Field(min_length=1, max_length=1024)
    # Feeds ``run_search``'s ``limit`` (the cross-encoder's top-N). The rerank
    # fan-out is 30, so values above that just return the full reranked pool.
    limit_chunks: int = Field(default=20, ge=1, le=100)


class Citation(BaseModel):
    """One resolvable source behind the summary — maps a marker to its chunk."""

    marker: str
    book_id: uuid.UUID
    title: str
    chunk_index: int
    filename: str | None = None
    parent_section: str | None = None


class SummaryResponse(BaseModel):
    summary: str
    citations: list[Citation]


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
    """
    provider = _active_provider()
    api_key = provider.api_key()
    if not api_key:
        msg = f"{provider.key_env_var} is not configured"
        raise RuntimeError(msg)
    return openai.OpenAI(base_url=provider.base_url, api_key=api_key)


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
    try:
        response = _client().chat.completions.create(
            model=settings.llm_model or provider.default_model,
            messages=[
                {"role": "system", "content": _SYSTEM_INSTRUCTION},
                {"role": "user", "content": _build_prompt(query, sources)},
            ],
            temperature=_TEMPERATURE,
            max_tokens=_MAX_OUTPUT_TOKENS,
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
        filename=source.filename,
        parent_section=source.parent_section,
    )


def _extract_citations(summary_text: str, sources: Sequence[_Source]) -> list[Citation]:
    """Return the sources whose markers appear in *summary_text*, first-appearance order.

    Only markers we handed the model resolve — a marker the model paraphrases
    or invents is ignored, so the citation list never carries an unresolvable
    or hallucinated reference. The trade-off is that an off-format marker
    drops a real citation; acceptable for v0, where the grounding instruction
    pins the exact marker strings. Markers are bracket-delimited, so no marker
    is a substring of another (``[X:1]`` cannot match inside ``[X:12]``).
    """
    appearances: list[tuple[int, _Source]] = []
    for source in sources:
        idx = summary_text.find(source.marker)
        if idx != -1:
            appearances.append((idx, source))
    appearances.sort(key=lambda item: item[0])
    return [_to_citation(source) for _, source in appearances]


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


@router.post("", response_model=SummaryResponse)
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

    hits = await run_search(
        query=payload.query,
        limit=payload.limit_chunks,
        do_rerank=True,
        user_id=current_user.user_id,
        session=session,
    )
    if not hits:
        # Deterministic half of the hallucination guard: nothing retrieved →
        # say so, never call the LLM.
        return SummaryResponse(summary=_NO_CONTEXT_MESSAGE, citations=[])

    titles = await _resolve_titles(session, [h.book_id for h in hits])
    sources = _build_sources(hits, titles)

    summary_text = await asyncio.to_thread(
        _generate_summary,
        query=payload.query,
        sources=sources,
    )
    citations = _extract_citations(summary_text, sources)
    return SummaryResponse(summary=summary_text, citations=citations)
