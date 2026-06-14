"""Markdown → semantic chunks for embedding.

`chunk(markdown)` returns a list of `Chunk` rows whose boundaries fall on
sentence ends and shifts in meaning, not fixed token windows. The splitter
embeds adjacent sentence groups and breaks where cosine distance jumps past
a percentile threshold — see LlamaIndex `SemanticSplitterNodeParser`.

The boundary-detection embedder is `BAAI/bge-large-en-v1.5`, the same model
used downstream in Phase 6 for the chunk embeddings that land in Milvus
(ARCHITECTURE.md §2). Since Phase 16b (ADR 0006) it is a remote call: the
llama-index OpenAI-compatible embedding class talks to the same endpoint +
model id `worker/inference.py` uses, so boundary placement stays calibrated
to the exact weights that embed the chunks — and ingest no longer loads a
~1.3GB model (or spikes ~3GB RSS) in-process.

`parent_section` is a best-effort lookup: for each chunk, find the most
recent Markdown ATX heading (`#`, `##`, …) at or before the chunk's start
offset. This is a hint for citation UX, not a guarantee — a chunk that
starts inside a heading-less preamble will have `parent_section=None`.
Heading text is passed through `clean_heading` at capture, so the stored
value (Postgres `chunks.parent_section` and Milvus row metadata alike) is
plain text — never pandoc's inline-HTML tag soup.

CLI (run from `worker/`):

    uv run python -m chunking path/to/book.md
"""

# llama-index ships without PEP 561 markers; relax the strict-unknown rules
# locally rather than papering over the import sites.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import cast

from llama_index.core import Document
from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.node_parser import SemanticSplitterNodeParser
from llama_index.core.schema import TextNode
from nltk.tokenize import PunktSentenceTokenizer

from inference import embed_texts, token_count, truncation_token_limit
from inference import settings as inference_settings

# BGE-Large is the locked retrieval embedder (ARCHITECTURE.md §2). Reading
# the same env-driven setting `inference.py` uses means boundary detection
# and chunk embedding can never disagree on model or endpoint (ADR 0006).
DEFAULT_EMBED_MODEL = inference_settings.embeddings_model

# SemanticSplitter defaults. `buffer_size=1` groups one sentence on each side
# of a candidate boundary before embedding; `breakpoint_percentile_threshold=95`
# splits where the cosine distance between neighbours sits in the top 5%.
# These are LlamaIndex's defaults; documented here so Phase 5+ readers know
# what knobs exist.
_BUFFER_SENTENCES = 1
_BREAKPOINT_PERCENTILE = 95

# --- Oversized-chunk cap (sub-split pass) ------------------------------------
# Milvus stores `content_chunk` as VARCHAR(65535) where the limit is BYTES of
# UTF-8 (worker/scripts/bootstrap_milvus.py). The SemanticSplitter places
# boundaries on shifts in MEANING, not on size — a large homogeneous run (a
# concordance, an index, a long table) can collapse into a single multi-hundred-
# KB chunk whose `content_chunk` insert Milvus rejects outright (live: a
# 355687-char chunk from a seeded EPUB failed the corpus-seed ingest).
#
# `_HARD_MAX_CHUNK_BYTES` is Milvus's hard VARCHAR cap — every emitted sub-chunk
# MUST be at or under it (we do NOT raise the schema; we fit it). `_MAX_CHUNK_BYTES`
# is a safe trigger BELOW that hard cap: only chunks over this size are
# sub-split, leaving comfortable margin so a normal book is never touched and a
# borderline chunk is split with room to spare. Chunks at or under the trigger
# are returned byte-identical — the common path pays one `len(text.encode())`
# and nothing else.
_HARD_MAX_CHUNK_BYTES = 65535
_MAX_CHUNK_BYTES = 60000

# Soft per-sub-chunk token target. The embedder truncates each input to
# `truncation_token_limit()` (510) content tokens, so a sub-chunk over that
# would be embedded head-only — its stored tail would never influence its
# vector. Sizing sub-chunks to <= that window makes the stored text match what
# the embedder actually encodes for the oversized chunks we touch.
_MAX_CHUNK_TOKENS = truncation_token_limit()


@lru_cache(maxsize=1)
def _sentence_tokenizer() -> PunktSentenceTokenizer:
    """Punkt sentence tokenizer for the sub-split pass, built once per process.

    Default (untrained) Punkt parameters — the same English model the
    SemanticSplitter's own sentence buffering rides — so no NLTK corpus
    download and no network. ``span_tokenize`` yields offset spans, which is
    what lets the sub-split reconstruct valid windows into the original text.
    """
    return PunktSentenceTokenizer()


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Partition *text* into contiguous ``(start, end)`` sentence windows.

    Built from Punkt's ``span_tokenize`` *starts* (not its end offsets) so the
    pieces tile the WHOLE string with no gaps and no overlap — inter-sentence
    whitespace attaches to the preceding sentence, leading whitespace to the
    first. The invariant the caller relies on:
    ``"".join(text[s:e] for s, e in _sentence_spans(text)) == text``.

    Empty text yields no spans; text Punkt finds no sentence boundary in (one
    long run with no terminator) yields a single ``(0, len(text))`` span, which
    the byte/token caps then hard-split as a last resort.
    """
    if not text:
        return []
    starts = [s for s, _ in _sentence_tokenizer().span_tokenize(text)]
    if not starts or starts[0] != 0:
        # Punkt drops leading whitespace from its first span; anchor at 0 so
        # nothing is lost. A boundary-less run gives no starts at all.
        starts = [0, *starts] if starts else [0]
    bounds = [*starts[1:], len(text)]
    return [(start, end) for start, end in zip(starts, bounds, strict=True) if start < end]


def _window_fits(text: str, start: int, end: int) -> bool:
    """True when ``text[start:end]`` is within BOTH the byte and token caps.

    The byte cap (``_HARD_MAX_CHUNK_BYTES``) is Milvus's hard limit; the token
    cap (``_MAX_CHUNK_TOKENS``) keeps the stored text inside the embedder's
    window. Both are checked so a single window never exceeds either.
    """
    piece = text[start:end]
    return (
        len(piece.encode("utf-8")) <= _HARD_MAX_CHUNK_BYTES
        and token_count(piece) <= _MAX_CHUNK_TOKENS
    )


def _hard_windows(text: str, start_offset: int) -> list[tuple[int, int]]:
    """Hard-split *text* into ``(abs_start, abs_end)`` windows fitting both caps.

    Last resort for a single sentence that alone exceeds a cap (a giant run with
    no sentence terminator, or one pathological sentence). Each window is the
    longest CHARACTER prefix of the remaining text that fits both
    ``_HARD_MAX_CHUNK_BYTES`` (hard — Milvus VARCHAR) and ``_MAX_CHUNK_TOKENS``
    (soft — embedder window), found by binary search on the character count — so
    cuts always land on a UTF-8 CODEPOINT boundary (never mid-codepoint, since a
    Python string index is a codepoint index). Offsets are absolute
    (``start_offset`` + char index) so they stay valid windows into the original
    markdown, and the windows tile the text with no gaps or overlap. A single
    codepoint always fits both caps, so every window advances by >= 1 char.
    """
    windows: list[tuple[int, int]] = []
    n = len(text)
    i = 0
    while i < n:
        # Largest end in (i, n] with text[i:end] fitting both caps.
        lo, hi, best = i + 1, n, i + 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if _window_fits(text, i, mid):
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        windows.append((start_offset + i, start_offset + best))
        i = best
    return windows


def _split_chunk_text(text: str, base_offset: int, parent_section: str | None) -> list[Chunk]:
    """Sub-split one oversized chunk's *text* into capped, offset-true sub-chunks.

    Greedily packs whole sentences (``_sentence_spans``) into sub-chunks, opening
    a new sub-chunk before a sentence would push the running piece over either
    cap: ``_MAX_CHUNK_TOKENS`` content tokens (soft — keeps stored text aligned
    with what the embedder encodes) or ``_HARD_MAX_CHUNK_BYTES`` UTF-8 bytes
    (hard — Milvus's VARCHAR limit). A single sentence that alone exceeds a cap
    is itself windowed (``_hard_windows``, codepoint-safe, fitting both caps) so
    no sub-chunk can ever exceed either cap.

    Sub-chunks partition *text* contiguously: their offsets are absolute windows
    into the original markdown (``base_offset`` + char index) and concatenating
    their text reproduces *text* exactly. ``parent_section`` is carried onto
    every sub-chunk.
    """
    pieces: list[tuple[int, int]] = []  # (abs_start, abs_end) sub-sentence atoms
    for s_start, s_end in _sentence_spans(text):
        sentence = text[s_start:s_end]
        if not _window_fits(text, s_start, s_end):
            # One sentence over a cap: window it so each atom fits BOTH caps.
            pieces.extend(_hard_windows(sentence, base_offset + s_start))
        else:
            pieces.append((base_offset + s_start, base_offset + s_end))

    sub_chunks: list[Chunk] = []
    run_start: int | None = None
    run_end = 0
    abs_base = base_offset
    for abs_start, abs_end in pieces:
        if run_start is None:
            run_start, run_end = abs_start, abs_end
            continue
        candidate = text[run_start - abs_base : abs_end - abs_base]
        if (
            len(candidate.encode("utf-8")) > _HARD_MAX_CHUNK_BYTES
            or token_count(candidate) > _MAX_CHUNK_TOKENS
        ):
            sub_chunks.append(
                Chunk(
                    text=text[run_start - abs_base : run_end - abs_base],
                    start_idx=run_start,
                    end_idx=run_end,
                    parent_section=parent_section,
                )
            )
            run_start, run_end = abs_start, abs_end
        else:
            run_end = abs_end
    if run_start is not None:
        sub_chunks.append(
            Chunk(
                text=text[run_start - abs_base : run_end - abs_base],
                start_idx=run_start,
                end_idx=run_end,
                parent_section=parent_section,
            )
        )
    return sub_chunks


def _cap_oversized_chunks(chunks: list[Chunk]) -> list[Chunk]:
    """Sub-split any chunk whose UTF-8 byte length exceeds ``_MAX_CHUNK_BYTES``.

    Pure and keyless — counts tokens with the bundled local BGE tokenizer
    (``inference.token_count``) and bytes with ``str.encode``; no embedder, no
    network. Chunks at or under the trigger pass through BYTE-IDENTICAL (same
    object), so a normal book is completely unaffected. The SemanticSplitter
    sizes on meaning, not bytes, so a large homogeneous section can produce a
    single chunk over Milvus's 65535-byte ``content_chunk`` cap; this pass
    fits it without raising the schema (ARCHITECTURE.md §3).

    No text is lost: an oversized chunk's sub-chunks partition its text
    contiguously (concatenation reproduces the original) and carry its
    ``parent_section`` and valid offset windows into the original markdown.
    """
    capped: list[Chunk] = []
    for c in chunks:
        if len(c.text.encode("utf-8")) <= _MAX_CHUNK_BYTES:
            capped.append(c)
            continue
        capped.extend(_split_chunk_text(c.text, c.start_idx, c.parent_section))
    return capped


# ATX-style Markdown header: 1–6 `#` followed by a space and the heading text.
# Setext headings (`Title\n=====`) are rare in pandoc/pymupdf4llm output and
# not worth the extra state to support.
_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Chunk:
    """One semantic chunk of a markdown document.

    `start_idx`/`end_idx` are character offsets into the original markdown
    so callers can reconstruct context, attach highlights, or render a
    citation pin. `parent_section` is the nearest enclosing ATX heading
    text (without leading `#`s, stripped of inline HTML via
    `clean_heading`), or `None` if the chunk falls before any heading.
    """

    text: str
    start_idx: int
    end_idx: int
    parent_section: str | None


class _TagStripper(HTMLParser):
    """Collects the text content of an HTML fragment, dropping tags.

    ``convert_charrefs=True`` (the stdlib default, set explicitly here)
    makes the parser hand ``handle_data`` already-unescaped text, so
    entities are decoded exactly once — a separate ``html.unescape`` pass
    would double-unescape (``&amp;lt;`` → ``<`` instead of ``&lt;``).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def clean_heading(raw: str) -> str:
    """Strip HTML markup from captured heading text; collapse whitespace.

    pandoc's GFM output keeps inline HTML from EPUB headings (anchors,
    spans), and its ~72-col line wrapping means the per-line ATX regex can
    capture tag soup truncated mid-tag (``<a href="..."><span``). A real
    HTML parser — not a regex — survives nested tags, ``>`` inside quoted
    attribute values, and plain text that merely looks like markup
    (``a < b`` is preserved verbatim; an unterminated trailing tag
    fragment is discarded). Entities are unescaped exactly once and
    whitespace runs collapse to single spaces.

    May return ``""`` (anchor-only headings, tag-only truncated
    fragments). Callers must treat that as "no heading": `_heading_offsets`
    drops empty headings from its list, so affected chunks fall back to the
    nearest preceding real heading, or ``None`` when none exists — the
    empty string is never stored as a ``parent_section``.

    Maintenance/backfill scripts that clean stored ``parent_section``
    values MUST import and reuse this function so capture-time and
    backfill-time semantics can never drift.
    """
    parser = _TagStripper()
    parser.feed(raw)
    parser.close()
    return " ".join("".join(parser.parts).split())


def _heading_offsets(markdown: str) -> list[tuple[int, str]]:
    """Return `(start_offset, heading_text)` for every ATX heading, in order.

    Heading text is passed through `clean_heading`; headings that strip to
    the empty string (anchor-only inline HTML, truncated tag fragments) are
    omitted so the parent-section lookup falls back to the previous real
    heading — both Postgres `chunks` rows and Milvus metadata derive from
    the values produced here.
    """
    offsets: list[tuple[int, str]] = []
    for m in _ATX_HEADING.finditer(markdown):
        if cleaned := clean_heading(m.group(2)):
            offsets.append((m.start(), cleaned))
    return offsets


def _parent_section_for(offset: int, headings: list[tuple[int, str]]) -> str | None:
    """Return the most recent heading text at or before *offset*, or None."""
    last: str | None = None
    for start, text in headings:
        if start > offset:
            break
        last = text
    return last


class _RemoteBGEEmbedding(BaseEmbedding):
    """llama-index embedding adapter over ``inference.embed_texts`` (ADR 0006).

    Routes the semantic splitter's boundary embeddings through the SAME remote
    transport the chunk embeddings use — same model id, same 512-token
    truncation — so boundary detection stays calibrated to the exact weights
    that embed the chunks, and no model loads in-process. The splitter only
    needs text embeddings (cosine between adjacent sentence groups); the query
    methods are implemented for interface completeness. ``embed_texts`` raises
    ``MissingInferenceKeyError`` lazily when the key is unset, so import / lint
    / tests never need a key.
    """

    def _get_query_embedding(self, query: str) -> list[float]:
        return embed_texts([query], model=DEFAULT_EMBED_MODEL)[0].tolist()

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._get_query_embedding(query)

    def _get_text_embedding(self, text: str) -> list[float]:
        return embed_texts([text], model=DEFAULT_EMBED_MODEL)[0].tolist()

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return [row.tolist() for row in embed_texts(texts, model=DEFAULT_EMBED_MODEL)]


@lru_cache(maxsize=1)
def _default_embedder() -> _RemoteBGEEmbedding:
    """The remote boundary embedder, constructed once per process (ADR 0006).

    A thin transport adapter — no weights, no key needed at construction
    (``embed_texts`` validates the key lazily on first call). ``embed_batch_size``
    is raised so the splitter sends sentence groups in larger batches, cutting
    round-trips on a book-sized ingest.
    """
    return _RemoteBGEEmbedding(embed_batch_size=128)


def chunk(markdown: str) -> list[Chunk]:
    """Split *markdown* into semantic chunks.

    Boundaries fall on sentence ends; passages whose meaning shifts (high
    inter-sentence embedding distance) become separate chunks. Uses
    BGE-Large for boundary detection, loaded once per process.

    A post-process pass (``_cap_oversized_chunks``) then sub-splits any chunk
    over ``_MAX_CHUNK_BYTES`` UTF-8 bytes so every returned chunk fits Milvus's
    65535-byte ``content_chunk`` cap (the SemanticSplitter sizes on meaning, not
    bytes, so a large homogeneous section can otherwise produce one chunk too
    big to insert). The pass is pure and keyless — local-tokenizer counts only,
    no extra embedder calls — and leaves normal-sized chunks byte-identical, so
    typical books are unaffected.

    Returns an empty list for empty input — the splitter would otherwise
    raise on a zero-length document.
    """
    if not markdown.strip():
        return []

    splitter = SemanticSplitterNodeParser.from_defaults(
        buffer_size=_BUFFER_SENTENCES,
        breakpoint_percentile_threshold=_BREAKPOINT_PERCENTILE,
        embed_model=_default_embedder(),
    )
    nodes = splitter.get_nodes_from_documents([Document(text=markdown)])

    headings = _heading_offsets(markdown)
    chunks: list[Chunk] = []
    for node in nodes:
        # `get_nodes_from_documents` is typed `-> list[BaseNode]`, but the
        # semantic splitter always emits `TextNode`s with `start_char_idx`
        # / `end_char_idx`. The cast keeps pyright honest without a runtime
        # isinstance check we'd never expect to fail.
        text_node = cast(TextNode, node)
        text = text_node.get_content()
        start = text_node.start_char_idx
        end = text_node.end_char_idx
        if start is None or end is None:
            start = markdown.find(text)
            end = start + len(text) if start >= 0 else -1
        chunks.append(
            Chunk(
                text=text,
                start_idx=start,
                end_idx=end,
                parent_section=_parent_section_for(start, headings) if start >= 0 else None,
            )
        )
    return _cap_oversized_chunks(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="chunking",
        description="Semantic-chunk a Markdown file and print a preview of each chunk.",
    )
    parser.add_argument("path", help="Path to a Markdown file (e.g. Phase 4 extractor output).")
    parser.add_argument(
        "--preview-chars",
        type=int,
        default=120,
        help="Characters to show from the start of each chunk (default: 120).",
    )
    args = parser.parse_args(argv)

    markdown = Path(args.path).read_text(encoding="utf-8")
    chunks = chunk(markdown)
    sys.stdout.write(f"{len(chunks)} chunks\n")
    for i, c in enumerate(chunks):
        head = c.text[: args.preview_chars].replace("\n", " ")
        section = c.parent_section or "—"
        sys.stdout.write(f"[{i:04d}] {c.start_idx:>8}–{c.end_idx:<8} §{section} :: {head}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
