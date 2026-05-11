"""EPUB → Markdown via EbookLib + pandoc.

EbookLib walks the book's spine in reading order and yields each XHTML
document item. We concatenate those into one HTML blob and hand it to
pandoc for the conversion to GitHub-flavored Markdown.

This route was chosen over Apache Tika to avoid the alt-text and metadata
leakage Tika introduces (see ARCHITECTURE.md §2).
"""

# Neither EbookLib nor pypandoc ships PEP 561 type stubs; relax the missing-
# stub rules locally so the rest of the worker stays under strict pyright.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

from pathlib import Path

import pypandoc
from ebooklib import ITEM_DOCUMENT, epub


def to_markdown(path: str | Path) -> str:
    """Read *path* (an EPUB file) and return its body as Markdown."""
    book = epub.read_epub(str(path), options={"ignore_ncx": True})

    items_by_id = {item.get_id(): item for item in book.get_items_of_type(ITEM_DOCUMENT)}

    html_chunks: list[str] = []
    for entry in book.spine:
        # spine entries are (id, linear) tuples, but EbookLib has been known
        # to hand back bare ids in some EPUBs — accept both shapes.
        item_id = entry[0] if isinstance(entry, tuple) else entry
        item = items_by_id.get(item_id)
        if item is None:
            continue
        content: bytes = item.get_content()
        html_chunks.append(content.decode("utf-8", errors="replace"))

    html = "\n\n".join(html_chunks)
    markdown: str = pypandoc.convert_text(html, to="gfm", format="html")
    return markdown
