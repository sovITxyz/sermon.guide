"""PDF → Markdown via pymupdf4llm.

`pymupdf4llm.to_markdown` is markdown-aware and preserves page structure
(headings, lists, tables-as-pipe-tables when it can detect them) far better
than naïve text extraction from PyMuPDF alone.
"""

# pymupdf4llm has no PEP 561 marker; pyright can't see into its API.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

from pathlib import Path

import pymupdf4llm


def to_markdown(path: str | Path) -> str:
    """Read *path* (a PDF file) and return its body as Markdown."""
    # pymupdf4llm.to_markdown's return type is `str | list[dict]` (the latter
    # when page_chunks=True). We never opt into chunked output, so narrow the
    # union explicitly rather than letting an `isinstance` assumption ride.
    result = pymupdf4llm.to_markdown(str(path), page_chunks=False)
    if not isinstance(result, str):
        msg = f"pymupdf4llm returned non-str output for {path}: {type(result).__name__}"
        raise TypeError(msg)
    return result
