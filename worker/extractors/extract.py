"""Format detection + extraction dispatcher.

`detect(path)` sniffs the MIME via libmagic — file extensions are NEVER
trusted, since the ingestion pipeline will eventually accept uploads from
untrusted users and a renamed `malicious.epub` is the kind of thing that
ends in CVEs.

`extract(path)` dispatches to the right per-format extractor and returns
clean Markdown.

CLI (run from `worker/`):

    uv run python -m extractors path/to/book.epub
"""

# python-magic is a thin ctypes wrapper around libmagic and has no PEP 561
# marker. Relax the stub-related rules locally.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Literal

import magic

from . import epub as epub_extractor
from . import pdf as pdf_extractor

Format = Literal["epub", "pdf"]

_MIME_TO_FORMAT: dict[str, Format] = {
    "application/epub+zip": "epub",
    "application/pdf": "pdf",
}


class UnsupportedFormatError(ValueError):
    """Raised when the input file's MIME type is not EPUB or PDF."""


def detect(path: str | Path) -> Format:
    """Return the canonical short name of *path*'s format.

    Sniffs MIME via libmagic; ignores the file extension.

    Raises:
        FileNotFoundError: *path* does not exist.
        UnsupportedFormatError: MIME is not application/epub+zip or
            application/pdf.
    """
    p = Path(path)
    if not p.is_file():
        msg = f"not a file: {p}"
        raise FileNotFoundError(msg)
    mime: str = magic.from_file(str(p), mime=True)
    fmt = _MIME_TO_FORMAT.get(mime)
    if fmt is None:
        msg = f"unsupported MIME type {mime!r} for {p}"
        raise UnsupportedFormatError(msg)
    return fmt


def extract(path: str | Path) -> str:
    """Extract clean Markdown from *path*.

    Dispatches on `detect(path)`.
    """
    fmt = detect(path)
    if fmt == "epub":
        return epub_extractor.to_markdown(path)
    return pdf_extractor.to_markdown(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="worker.extractors.extract",
        description="Extract clean Markdown from an EPUB or PDF.",
    )
    parser.add_argument("path", help="Path to an .epub or .pdf file.")
    args = parser.parse_args(argv)
    sys.stdout.write(extract(args.path))
    return 0
