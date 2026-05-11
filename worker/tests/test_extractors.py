"""Smoke tests for the extractors package.

Two layers:

1. **Detection** — pure unit tests that synthesize files with the right
   magic bytes. These run everywhere, including CI without samples.
2. **End-to-end extraction** — real EPUB/PDF books in
   `worker/tests/samples/`. These are the load-bearing checks (does the
   extractor actually produce sensible Markdown?), but the samples are
   gitignored (copyright); the suite skips cleanly when they're absent.

"Sane character distribution" means: predominantly printable, mostly
letters and whitespace, no large runs of binary garbage. We deliberately
don't pin the output exactly — pandoc and pymupdf4llm change formatting
across releases — but the ratios catch the kind of regression where the
extractor returns raw HTML, encoded gibberish, or an empty string.
"""

# pypandoc has no PEP 561 marker; only used inside one skip-guard.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false

from __future__ import annotations

import string
from pathlib import Path

import pytest

from extractors import UnsupportedFormatError, detect, extract

SAMPLES = Path(__file__).resolve().parent / "samples"
EPUB_SAMPLE = SAMPLES / "sample.epub"
PDF_SAMPLE = SAMPLES / "sample.pdf"

# A book chapter is at least a few hundred characters even for a pamphlet;
# anything smaller suggests the extractor returned a stub.
MIN_BODY_CHARS = 500


def _printable_ratio(text: str) -> float:
    if not text:
        return 0.0
    printable = set(string.printable)
    return sum(1 for c in text if c in printable) / len(text)


def _letter_ratio(text: str) -> float:
    if not text:
        return 0.0
    return sum(1 for c in text if c.isalpha()) / len(text)


def _assert_sane_markdown(markdown: str, *, source: Path) -> None:
    """Cheap distribution checks on extracted Markdown."""
    assert markdown, f"{source} produced empty output"
    assert len(markdown) >= MIN_BODY_CHARS, (
        f"{source}: only {len(markdown)} chars extracted (< {MIN_BODY_CHARS})"
    )
    printable = _printable_ratio(markdown)
    letters = _letter_ratio(markdown)
    assert printable >= 0.95, f"{source}: only {printable:.2%} of chars are printable"
    assert letters >= 0.5, (
        f"{source}: only {letters:.2%} of chars are letters — extractor likely returned junk"
    )


def test_detect_pdf_from_magic_bytes(tmp_path: Path) -> None:
    """libmagic identifies a minimal `%PDF-` blob as application/pdf."""
    pdf = tmp_path / "x.bin"  # extension intentionally not .pdf
    # Smallest viable PDF — header + one object + xref + trailer. libmagic
    # only needs the leading `%PDF-` signature to classify it.
    pdf.write_bytes(
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog>>endobj\n"
        b"xref\n0 1\n0000000000 65535 f\n"
        b"trailer<</Size 1/Root 1 0 R>>\nstartxref\n0\n%%EOF\n"
    )
    assert detect(pdf) == "pdf"


def test_detect_rejects_unknown_format(tmp_path: Path) -> None:
    junk = tmp_path / "notes.txt"
    junk.write_text("hello world\n")
    with pytest.raises(UnsupportedFormatError):
        detect(junk)


def test_detect_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        detect(tmp_path / "does-not-exist.epub")


@pytest.mark.skipif(not EPUB_SAMPLE.exists(), reason="no EPUB sample in worker/tests/samples/")
def test_extract_epub_sample() -> None:
    """Real EPUB → readable Markdown via EbookLib + pandoc."""
    pytest.importorskip("pypandoc")
    import pypandoc

    try:
        pypandoc.get_pandoc_version()
    except OSError:
        pytest.skip("pandoc binary not installed (apt install pandoc)")

    assert detect(EPUB_SAMPLE) == "epub"
    md = extract(EPUB_SAMPLE)
    _assert_sane_markdown(md, source=EPUB_SAMPLE)


@pytest.mark.skipif(not PDF_SAMPLE.exists(), reason="no PDF sample in worker/tests/samples/")
def test_extract_pdf_sample() -> None:
    """Real PDF → readable Markdown via pymupdf4llm."""
    assert detect(PDF_SAMPLE) == "pdf"
    md = extract(PDF_SAMPLE)
    _assert_sane_markdown(md, source=PDF_SAMPLE)
