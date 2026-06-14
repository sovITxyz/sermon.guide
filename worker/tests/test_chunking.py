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

from chunking import (
    _HARD_MAX_CHUNK_BYTES,
    _MAX_CHUNK_BYTES,
    _MAX_CHUNK_TOKENS,
    Chunk,
    _cap_oversized_chunks,
    _heading_offsets,
    _parent_section_for,
    chunk,
    clean_heading,
)
from inference import token_count

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


# ---------------------------------------------------------------------------
# Oversized-chunk sub-split (the `_cap_oversized_chunks` post-process pass).
#
# Pure + keyless: these drive the sub-split helper directly with synthetic
# Chunks, never the remote embedder. The live bug they regress: the
# SemanticSplitter sizes on meaning, not bytes, so a large homogeneous section
# can produce a single chunk over Milvus's 65535-byte `content_chunk` cap and
# the Milvus insert is rejected (live: a 355687-char chunk from a seeded EPUB).


def _assert_subchunks_valid(
    out: list[Chunk],
    *,
    original: str,
    base_offset: int,
    parent_section: str | None,
) -> None:
    """Every invariant the sub-split pass must hold for an oversized chunk."""
    assert out, "oversized chunk produced no sub-chunks"
    for c in out:
        # HARD: never exceed Milvus's VARCHAR byte cap.
        assert len(c.text.encode("utf-8")) <= _HARD_MAX_CHUNK_BYTES, (
            f"sub-chunk {len(c.text.encode('utf-8'))} bytes > {_HARD_MAX_CHUNK_BYTES}"
        )
        # SOFT: stay inside the embedder's token window so stored text matches
        # what the embedder actually encodes.
        assert token_count(c.text) <= _MAX_CHUNK_TOKENS, (
            f"sub-chunk {token_count(c.text)} tokens > {_MAX_CHUNK_TOKENS}"
        )
        # parent_section carried onto every sub-chunk.
        assert c.parent_section == parent_section
        # Codepoint-safe: text round-trips through UTF-8 with no broken
        # multibyte sequence (a mid-codepoint cut would raise / differ).
        assert c.text.encode("utf-8").decode("utf-8") == c.text
    # No text loss: sub-chunks reproduce the original exactly.
    assert "".join(c.text for c in out) == original
    # Offsets are valid, contiguous, non-overlapping windows into the source.
    prev = base_offset
    for c in out:
        assert c.start_idx == prev, "sub-chunks not contiguous"
        assert c.start_idx < c.end_idx, "empty/inverted sub-chunk window"
        assert c.text == original[c.start_idx - base_offset : c.end_idx - base_offset]
        prev = c.end_idx
    assert prev == base_offset + len(original), "sub-chunks do not cover the whole chunk"


def test_cap_oversized_subsplits_homogeneous_and_giant_sentence() -> None:
    """A long homogeneous run AND a single >cap sentence both get capped.

    Mirrors the live failure shape: a big block of similar sentences (the
    SemanticSplitter would emit it as one chunk) followed by one boundary-less
    run longer than the byte cap (the last-resort hard split's job)."""
    sentence = "The reader paused to consider the deep meaning of this passage before moving on. "
    homogeneous = sentence * 1500  # ~120 KB of many similar sentences
    giant_sentence = "word " * 20000  # ~100 KB single run with no terminator
    original = homogeneous + giant_sentence
    base = 1000  # not zero — exercises the offset arithmetic
    oversized = Chunk(
        text=original,
        start_idx=base,
        end_idx=base + len(original),
        parent_section="Concordance",
    )

    out = _cap_oversized_chunks([oversized])

    assert len(out) > 1, "an oversized chunk must be split into several sub-chunks"
    _assert_subchunks_valid(out, original=original, base_offset=base, parent_section="Concordance")


def test_cap_oversized_leaves_normal_chunks_byte_identical() -> None:
    """Chunks at/under the trigger pass through unchanged — normal books untouched."""
    normal = [
        Chunk(text="Short opening line.", start_idx=0, end_idx=19, parent_section="Intro"),
        Chunk(text="A second small chunk follows.", start_idx=20, end_idx=49, parent_section=None),
        # Exactly at the trigger byte size — must NOT be split (the cap is
        # "exceeds", not "reaches").
        Chunk(
            text="a" * _MAX_CHUNK_BYTES,
            start_idx=50,
            end_idx=50 + _MAX_CHUNK_BYTES,
            parent_section="Big",
        ),
    ]
    out = _cap_oversized_chunks(normal)

    assert len(out) == len(normal), "no sub-splitting should be triggered"
    # Byte-identical AND same objects — the common path returns inputs verbatim.
    for original, returned in zip(normal, out, strict=True):
        assert returned is original


def test_cap_oversized_just_over_trigger_is_split() -> None:
    """One byte over the trigger flips sub-splitting on; text is preserved."""
    text = "a" * (_MAX_CHUNK_BYTES + 1)
    oversized = Chunk(text=text, start_idx=0, end_idx=len(text), parent_section=None)
    out = _cap_oversized_chunks([oversized])
    assert "".join(c.text for c in out) == text
    _assert_subchunks_valid(out, original=text, base_offset=0, parent_section=None)


def test_cap_oversized_multibyte_never_splits_a_codepoint() -> None:
    """A multibyte (4-byte emoji) oversized run stays codepoint-safe under the cap."""
    # One boundary-less run of 4-byte codepoints, well over the byte cap, that
    # exercises the hard-window last resort on multibyte text.
    original = "😀" * 30000  # 120 000 bytes, no sentence boundary
    base = 5
    oversized = Chunk(
        text=original, start_idx=base, end_idx=base + len(original), parent_section="Emoji"
    )

    out = _cap_oversized_chunks([oversized])

    assert len(out) > 1
    _assert_subchunks_valid(out, original=original, base_offset=base, parent_section="Emoji")
    # Belt-and-suspenders: the raw bytes of each sub-chunk are independently
    # valid UTF-8 (a mid-codepoint cut would leave a truncated 4-byte sequence).
    for c in out:
        c.text.encode("utf-8").decode("utf-8")  # raises on a broken codepoint


def test_cap_oversized_empty_input_is_empty() -> None:
    """No chunks in, no chunks out — the pass is a no-op on an empty list."""
    assert _cap_oversized_chunks([]) == []


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
