"""Gemini 1.5 Flash summary agent — grounded 1–2 paragraph synthesis with citations.

Phase 14 (ARCHITECTURE.md §1 goal, §2 "LLM" row, §5 lifecycle final step).
``POST /search-summary`` is the user-facing endpoint the whole platform
builds toward: a natural-language question in, a short grounded answer with
citations back to the user's own books out.

## Pipeline reuse

This module does **not** re-implement retrieval. It calls
``search.run_search(do_rerank=True)`` — the exact Phase 12→13 path the
canonical ``POST /search`` uses (hybrid dense+sparse → RRF → cross-encoder
rerank → BGE-M3 sentence pruning). What comes back is already the
precision-ranked, token-pruned context; this module's only new work is
turning those chunks into a citation-marked prompt, calling Gemini, and
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
   no citations and never call Gemini.
2. **Instructed** — for non-empty-but-weak context, the system instruction
   tells the model to use only the provided passages and to say so plainly
   when they don't address the question.

## Config & failure modes

- ``GOOGLE_API_KEY`` (unprefixed; see ``settings.py``) is required. The
  handler 503s up front when it's unset rather than burning ~30s of CPU
  rerank just to fail at the LLM call.
- The Gemini client is lazy + ``@lru_cache``'d, mirroring the model loaders
  in ``rerank.py`` / ``highlight.py`` — import / lint / test never need a key
  or network.
- A Gemini API error or an empty candidate becomes a 502. Other unexpected
  failures fall through to a 500, the same fail-loud posture the retrieval
  arms take (``search.py`` module docstring; ``api/AGENTS.md`` open gaps).

## Tenant surface

None new. ``run_search`` owns the load-bearing tenant filter (JWT
``user_id`` → ``user_library`` book set). The title lookup is keyed only by
book_ids that already cleared that filter, and ``global_books`` is
shared-by-design (ARCHITECTURE.md §3/§4 dedup) — a title is not user-scoped
data. Gemini sees only chunk text the user is already authorized to read.

## Latency

Warm reranked retrieval is ~30s on CPU (``api/AGENTS.md`` open gap) before
Gemini is even called, so the ``/search-summary`` E2E is that plus the LLM
round-trip — well over the ARCHITECTURE.md §1 ``< 1s`` target. Acceptable for
sermon prep; the architecture-locked GPU swap is the documented path.
"""

# google-genai ships py.typed, so this module needs no file-wide stub
# relaxation like the pymilvus / sentence-transformers modules do — just one
# targeted ignore at the generate_content call (see the comment there).

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache

from db import GlobalBook
from fastapi import APIRouter, HTTPException, status
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import CurrentUserDep, SessionDep
from search import SearchHit, run_search
from settings import settings

router = APIRouter(prefix="/search-summary", tags=["summary"])

# Swap point if Gemini 1.5 Flash is retired: this is the only model literal.
GEMINI_MODEL = "gemini-1.5-flash"

# Low temperature for grounded, low-confabulation synthesis; 1–2 paragraphs
# with inline markers fit comfortably under the output cap.
_TEMPERATURE = 0.2
_MAX_OUTPUT_TOKENS = 768

# Cap on a citation label's length so a pathologically long title doesn't
# bloat every marker in the prompt.
_MAX_LABEL_LEN = 80

_NO_CONTEXT_MESSAGE = (
    "I couldn't find anything in your library that addresses that question."
)

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
def _client() -> genai.Client:
    """Construct the Gemini client once per process. Requires GOOGLE_API_KEY.

    Lazy + cached so import / lint / tests never need a key or network — only
    the first ``/search-summary`` that actually reaches Gemini pays the
    construction. The handler validates the key up front and 503s when unset,
    so this never runs unconfigured in practice; the guard keeps that
    invariant explicit (``lru_cache`` does not cache the raised exception, so
    setting the key later still works).
    """
    api_key = settings.google_api_key
    if not api_key:
        msg = "GOOGLE_API_KEY is not configured"
        raise RuntimeError(msg)
    return genai.Client(api_key=api_key)


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
    """Call Gemini with the grounded prompt. Blocking; offload via ``to_thread``.

    Raises ``HTTPException`` (502) on an upstream API error or an empty
    candidate — FastAPI re-raises it from the worker thread into the handler.
    """
    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_INSTRUCTION,
        temperature=_TEMPERATURE,
        max_output_tokens=_MAX_OUTPUT_TOKENS,
    )
    try:
        # `generate_content`'s `contents` union includes an optional-Pillow
        # image type pyright can't resolve (Pillow isn't an api dep), which
        # marks the member "partially unknown". We pass a plain str, so a
        # targeted ignore is enough — no file-wide relaxation needed.
        response = _client().models.generate_content(  # pyright: ignore[reportUnknownMemberType]
            model=GEMINI_MODEL,
            contents=_build_prompt(query, sources),
            config=config,
        )
    except errors.APIError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Summary generation failed upstream.",
        ) from exc
    text = response.text
    if text is None or not text.strip():
        # Empty candidate (e.g. a safety filter blocked the response). Fail
        # loud rather than fabricate a summary.
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
    pruned chunks, and asks Gemini 1.5 Flash to synthesize a grounded answer
    (ARCHITECTURE.md §5 final step). See the module docstring for the citation
    contract, the hallucination guard, and the tenant surface (inherited
    wholesale from ``run_search``).
    """
    if not settings.google_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Summary service is not configured.",
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
