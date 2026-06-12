"""Smoke tests for the semantic chunker.

Three layers, matching `test_extractors.py`:

1. **Pure unit** — synthetic markdown drives the ATX-header helper and the
   empty-input short-circuit. No embedder, no network, runs in CI.
2. **API surface** — `Chunk` dataclass shape and `chunk()` signature.
3. **End-to-end** — real EPUB from `worker/tests/samples/` extracted to
   markdown then chunked. Boundary count, ordering, and sentence-end shape
   are asserted. Skipped without a sample (copyrighted; gitignored) or
   without `DEEPINFRA_API_KEY` (Phase 16b: boundary embedding is a remote
   call — ~$0.01 of embeddings per run on a novel-sized EPUB).
"""

# Tests deliberately reach for `_heading_offsets` / `_parent_section_for` —
# they're the cheapest covered path for the parent-section logic and are
# fixtures of the chunking module's behaviour even if not its public API.
# pyright: reportPrivateUsage=false, reportMissingTypeStubs=false, reportUnknownMemberType=false

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from chunking import Chunk, _heading_offsets, _parent_section_for, chunk, clean_heading

SAMPLES = Path(__file__).resolve().parent / "samples"
EPUB_SAMPLE = SAMPLES / "sample.epub"

# A "typical novel" range from the Phase 5 spec. The lower bound catches the
# regression where the splitter collapses everything into one node; the upper
# bound catches the regression where it degenerates into a per-sentence split.
EXPECTED_MIN_CHUNKS = 50
EXPECTED_MAX_CHUNKS = 500


def test_chunk_dataclass_is_frozen() -> None:
    """Chunks are immutable so downstream code can hash/cache them freely."""
    c = Chunk(text="hi", start_idx=0, end_idx=2, parent_section=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.text = "no"  # type: ignore[misc]


def test_chunk_empty_markdown_returns_empty_list() -> None:
    """No model load on empty input — short-circuit before the embedder runs."""
    assert chunk("") == []
    assert chunk("   \n\t  ") == []


def test_heading_offsets_finds_all_atx_levels() -> None:
    md = "# One\n\nbody\n\n## Two\n\nbody\n\n###### Six\n"
    offsets = _heading_offsets(md)
    assert [text for _, text in offsets] == ["One", "Two", "Six"]
    # Offsets must be strictly increasing — callers rely on this for the
    # "most recent heading at or before X" lookup.
    starts = [s for s, _ in offsets]
    assert starts == sorted(starts)


def test_heading_offsets_ignores_hash_in_prose() -> None:
    """`#tag` mid-line is not an ATX heading; requires line start + space."""
    md = "Paragraph mentioning #notatag and code `#foo`.\n\n# Real Heading\n"
    offsets = _heading_offsets(md)
    assert [text for _, text in offsets] == ["Real Heading"]


def test_parent_section_picks_most_recent_heading() -> None:
    md = "# Alpha\n\nbody alpha\n\n## Beta\n\nbody beta\n"
    headings = _heading_offsets(md)
    body_alpha_offset = md.index("body alpha")
    body_beta_offset = md.index("body beta")
    assert _parent_section_for(body_alpha_offset, headings) == "Alpha"
    assert _parent_section_for(body_beta_offset, headings) == "Beta"


def test_parent_section_before_first_heading_is_none() -> None:
    md = "Preamble text.\n\n# First Heading\n\nbody\n"
    headings = _heading_offsets(md)
    assert _parent_section_for(0, headings) is None


# Real parent_section debris captured from the dev DB (Phase 21). pandoc
# wraps long heading lines at ~72 cols and the ATX regex is per-line, so
# most of these are truncated mid-tag — the human-readable title sat on the
# continuation line and was never captured, hence the empty expectations.
LIVE_DEBRIS: list[tuple[str, str]] = [
    ('<a href="part0002.html#pt03ch_01" class="calibre4"><span', ""),
    ('<a href="part0002.html#atp_01" class="calibre4"><span class="bold">About', "About"),
    ('<a href="part0002.html#pt04ch_10" class="calibre4"><span', ""),
    ('<a href="part0002.html#pt01ch_03" class="calibre4"><span', ""),
    ('<a href="part0002.html#pt04ch_11" class="calibre4"><span', ""),
    ('<a href="part0002.html#pt04ch_06" class="calibre4"><span', ""),
]


@pytest.mark.parametrize(("raw", "expected"), LIVE_DEBRIS)
def test_clean_heading_live_debris_samples(raw: str, expected: str) -> None:
    """Every real dirty sample cleans to plain text with no markup left."""
    cleaned = clean_heading(raw)
    assert cleaned == expected
    assert "<" not in cleaned


def test_clean_heading_anchor_only_strips_to_empty() -> None:
    """Anchor-only headings carry no text — empty signals 'drop me'."""
    assert clean_heading('<span id="anchor_only"></span>') == ""


def test_clean_heading_nested_tags_and_gt_in_attribute() -> None:
    """A real parser survives nesting and `>` inside quoted attributes."""
    raw = '<a href="x.html#c1"><span class="bold">Chapter 11</span></a>'
    assert clean_heading(raw) == "Chapter 11"
    assert clean_heading('<a title="a>b"><span>Title</span></a>') == "Title"


def test_clean_heading_unescapes_entities_exactly_once() -> None:
    assert clean_heading("War &amp; Peace") == "War & Peace"
    # `&amp;lt;` must become `&lt;` (one unescape), never `<` (double).
    assert clean_heading("&amp;lt;not a tag&amp;gt;") == "&lt;not a tag&gt;"


def test_clean_heading_preserves_plain_text_lt() -> None:
    """`a < b` is prose, not markup — `<` only opens a tag when one follows."""
    assert clean_heading("a < b") == "a < b"
    assert clean_heading("5 < 7 < 9") == "5 < 7 < 9"


def test_clean_heading_collapses_whitespace() -> None:
    assert clean_heading("  Chapter\t 1:   The   Beginning  ") == "Chapter 1: The Beginning"
    assert clean_heading("<span>Two</span> <span>Words</span>") == "Two Words"


@pytest.mark.parametrize(
    "raw",
    [
        *[sample for sample, _ in LIVE_DEBRIS],
        "a < b",
        '<a href="x.html#c1"><span>Chapter 11</span></a>',
        "War &amp; Peace",
        "  spaced   out  ",
        "",
    ],
)
def test_clean_heading_is_idempotent(raw: str) -> None:
    once = clean_heading(raw)
    assert clean_heading(once) == once


def test_heading_offsets_strip_html_debris() -> None:
    """Capture path: parent_section flows through clean_heading, and
    anchor-only headings are dropped so chunks fall back to the previous
    real heading rather than ever storing an empty string."""
    md = (
        '# <a href="part0002.html#ch01" class="calibre4"><span>Chapter 1</span></a>\n'
        "\n"
        "body one\n"
        "\n"
        '## <span id="anchor_only"></span>\n'
        "\n"
        "body two\n"
    )
    headings = _heading_offsets(md)
    assert [text for _, text in headings] == ["Chapter 1"]
    assert _parent_section_for(md.index("body one"), headings) == "Chapter 1"
    # The anchor-only heading between the two bodies was dropped, so body
    # two falls back to the nearest preceding real heading.
    assert _parent_section_for(md.index("body two"), headings) == "Chapter 1"


def test_truncated_pandoc_heading_falls_back_to_previous_real_heading() -> None:
    """The live failure mode: pandoc wrapped a long heading, the regex
    captured an unterminated tag fragment, and it must not become a
    parent_section."""
    md = (
        "# Introduction\n"
        "\n"
        "intro body\n"
        "\n"
        '## <a href="part0002.html#pt03ch_11" class="calibre4"><span\n'
        "\n"
        "chapter body\n"
    )
    headings = _heading_offsets(md)
    assert [text for _, text in headings] == ["Introduction"]
    assert _parent_section_for(md.index("chapter body"), headings) == "Introduction"


def test_anchor_only_heading_before_any_real_heading_yields_none() -> None:
    """No real heading anywhere above -> None, never the empty string."""
    md = '# <span id="anchor"></span>\n\nbody\n'
    headings = _heading_offsets(md)
    assert headings == []
    assert _parent_section_for(md.index("body"), headings) is None


def _remote_embeddings_available() -> bool:
    """True when the remote boundary embedder can be reached (Phase 16b).

    The semantic splitter's embeddings are a remote call now; CI without
    the key skips the end-to-end test rather than failing on a 503.
    """
    return bool(os.environ.get("DEEPINFRA_API_KEY"))


@pytest.mark.skipif(not EPUB_SAMPLE.exists(), reason="no EPUB sample in worker/tests/samples/")
@pytest.mark.skipif(
    not _remote_embeddings_available(),
    reason="DEEPINFRA_API_KEY unset — remote boundary embedding unavailable",
)
def test_chunk_real_epub_produces_sane_chunks() -> None:
    """Real EPUB → 50–500 chunks with monotonically advancing offsets."""
    pytest.importorskip("pypandoc")
    import pypandoc

    try:
        pypandoc.get_pandoc_version()
    except OSError:
        pytest.skip("pandoc binary not installed (apt install pandoc)")

    from extractors import extract

    markdown = extract(EPUB_SAMPLE)
    chunks = chunk(markdown)

    assert EXPECTED_MIN_CHUNKS <= len(chunks) <= EXPECTED_MAX_CHUNKS, (
        f"got {len(chunks)} chunks; expected {EXPECTED_MIN_CHUNKS}–{EXPECTED_MAX_CHUNKS}"
    )

    # Every chunk has non-empty text and a valid offset window into the
    # source markdown.
    for c in chunks:
        assert c.text, "empty chunk text"
        assert 0 <= c.start_idx < c.end_idx <= len(markdown)
        assert markdown[c.start_idx : c.end_idx].strip() == c.text.strip() or c.text in markdown

    # Boundaries should land on sentence ends, not mid-word. We allow a small
    # tail (whitespace or a closing quote) but the last non-whitespace char
    # should be terminal punctuation for the vast majority of chunks.
    terminal = set(".!?\"')")
    sentence_end = sum(1 for c in chunks if c.text.rstrip()[-1:] in terminal)
    assert sentence_end / len(chunks) >= 0.8, (
        f"only {sentence_end}/{len(chunks)} chunks end on sentence boundaries"
    )

    # Offsets must be non-decreasing across the list — semantic splitter
    # walks the document in order.
    starts = [c.start_idx for c in chunks]
    assert starts == sorted(starts), "chunks emitted out of document order"
