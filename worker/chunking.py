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
from pathlib import Path
from typing import cast

from llama_index.core import Document
from llama_index.core.node_parser import SemanticSplitterNodeParser
from llama_index.core.schema import TextNode
from llama_index.embeddings.openai_like import OpenAILikeEmbedding

from inference import MissingInferenceKeyError
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
    text (without leading `#`s), or `None` if the chunk falls before any
    heading.
    """

    text: str
    start_idx: int
    end_idx: int
    parent_section: str | None


def _heading_offsets(markdown: str) -> list[tuple[int, str]]:
    """Return `(start_offset, heading_text)` for every ATX heading, in order."""
    return [(m.start(), m.group(2).strip()) for m in _ATX_HEADING.finditer(markdown)]


def _parent_section_for(offset: int, headings: list[tuple[int, str]]) -> str | None:
    """Return the most recent heading text at or before *offset*, or None."""
    last: str | None = None
    for start, text in headings:
        if start > offset:
            break
        last = text
    return last


@lru_cache(maxsize=1)
def _default_embedder() -> OpenAILikeEmbedding:
    """Construct the remote boundary embedder once per process (ADR 0006).

    Same endpoint + model id as `inference.embed_texts`; llama-index owns
    the batching for the splitter's sentence-group windows. One retry and
    an explicit timeout, matching the shared transport's posture. Lazy +
    cached so import / lint / tests never need a key — and `lru_cache`
    does not cache the raise, so setting the key later still works.
    """
    if not inference_settings.deepinfra_api_key:
        msg = "Remote inference is not configured; set DEEPINFRA_API_KEY."
        raise MissingInferenceKeyError(msg)
    return OpenAILikeEmbedding(
        model_name=DEFAULT_EMBED_MODEL,
        api_base=inference_settings.embeddings_base_url,
        api_key=inference_settings.deepinfra_api_key,
        max_retries=1,
        timeout=60.0,
    )


def chunk(markdown: str) -> list[Chunk]:
    """Split *markdown* into semantic chunks.

    Boundaries fall on sentence ends; passages whose meaning shifts (high
    inter-sentence embedding distance) become separate chunks. Uses
    BGE-Large for boundary detection, loaded once per process.

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
    return chunks


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
