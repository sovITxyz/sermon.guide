"""Format detection + extraction.

Public surface:

- `detect(path)` — sniff the file's MIME via libmagic and return the canonical
  short name (`"epub"` / `"pdf"`). Never trusts the file extension; the
  ingestion pipeline will eventually accept uploads from untrusted users.
- `extract(path)` — dispatch to the per-format extractor and return Markdown.
- `UnsupportedFormatError` — raised for anything that isn't EPUB or PDF.

CLI (run from `worker/`):

    uv run python -m extractors path/to/book.epub

See ARCHITECTURE.md §2 for the locked extraction-stack decisions and
worker/AGENTS.md for the contributor-facing notes.
"""

from __future__ import annotations

from .extract import Format, UnsupportedFormatError, detect, extract

__all__ = ["Format", "UnsupportedFormatError", "detect", "extract"]
