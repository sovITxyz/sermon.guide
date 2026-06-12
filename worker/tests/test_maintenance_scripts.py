"""Pure-unit tests for the Phase 21 maintenance scripts.

Covers the keyless, no-I/O seams of ``scripts.clean_parent_sections``
(dirty-detection predicate + Milvus row rewrite payloads) and
``scripts.sweep_orphans`` (orphan classification + Milvus expr-literal
allowlist). The destructive halves — live Postgres UPDATEs and Milvus
delete+reinsert — are exercised by operator dry-run/execute passes against
the dev stack, matching the split ``backfill_chunks`` uses (pure helpers
unit-tested; store mutations validated live).

The dirty fixtures mirror the live debris inventory: pandoc ``<a href=…>``
TOC-anchor tag soup from EPUB ingests, often truncated mid-tag by pandoc's
~72-column wrapping.
"""

from __future__ import annotations

import pytest

from scripts.clean_parent_sections import (
    corrected_milvus_row,
    is_dirty,
    target_parent_section,
)
from scripts.sweep_orphans import BookFacts, book_id_expr, classify

# ---------------------------------------------------------------------------
# clean_parent_sections — target_parent_section / is_dirty
# ---------------------------------------------------------------------------


def test_target_none_passes_through() -> None:
    assert target_parent_section(None) is None


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        '<span id="anchor_only"></span>',
        # Truncated mid-tag (pandoc 72-col wrap) — no text content survives.
        '<a href="part0002.html#pt03ch_01" class="calibre4"><span',
    ],
)
def test_target_empty_after_strip_becomes_none(raw: str) -> None:
    """'' is never a stored parent_section — empty-after-clean maps to NULL."""
    assert target_parent_section(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "Chapter 1: The Beginning",
        "About",
        "a < b",  # prose that merely looks like markup is preserved
    ],
)
def test_target_clean_values_unchanged(raw: str) -> None:
    assert target_parent_section(raw) == raw


def test_target_recovers_text_from_tag_soup() -> None:
    raw = '<a href="part0002.html#atp_01" class="calibre4"><span class="bold">About'
    assert target_parent_section(raw) == "About"


@pytest.mark.parametrize(
    "raw",
    [
        '<a href="part0002.html#pt03ch_01" class="calibre4"><span',
        '<a href="part0002.html#atp_01" class="calibre4"><span class="bold">About',
        "",  # legacy empty string must be rewritten to NULL
        "Two  spaces",  # whitespace collapse counts as dirty
    ],
)
def test_is_dirty_true_for_debris(raw: str) -> None:
    assert is_dirty(raw)


@pytest.mark.parametrize(
    "raw",
    [None, "Chapter 1", "a < b", "About"],
)
def test_is_dirty_false_for_clean_values(raw: str | None) -> None:
    assert not is_dirty(raw)


@pytest.mark.parametrize("raw", [123, ["x"], {"a": 1}, 1.5])
def test_is_dirty_false_for_non_strings(raw: object) -> None:
    """Milvus metadata is untyped JSON — non-strings are left alone."""
    assert not is_dirty(raw)


def test_is_dirty_converges_after_clean() -> None:
    """Idempotency seam: a cleaned value is never dirty on the second pass."""
    raw = '<a href="part0002.html#atp_01" class="calibre4"><span class="bold">About'
    assert not is_dirty(target_parent_section(raw))


# ---------------------------------------------------------------------------
# clean_parent_sections — corrected_milvus_row
# ---------------------------------------------------------------------------


def _milvus_row(parent_section: object) -> dict[str, object]:
    return {
        "id": 4242,
        "vector": [0.1, 0.2, 0.3],
        "book_id": "811f3136-b9bf-4ea3-a4e4-9687e3d26c60",
        "content_chunk": "In the beginning...",
        "metadata": {
            "filename": "sample.epub",
            "chunk_index": 7,
            "parent_section": parent_section,
        },
    }


def test_corrected_row_rewrites_only_parent_section() -> None:
    row = _milvus_row('<span class="bold">About</span>')
    fixed = corrected_milvus_row(row)
    assert fixed is not None
    metadata = fixed["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["parent_section"] == "About"
    assert metadata["filename"] == "sample.epub"
    assert metadata["chunk_index"] == 7


def test_corrected_row_drops_auto_id_pk() -> None:
    """auto_id collections refuse a supplied PK — `id` must not be reinserted."""
    fixed = corrected_milvus_row(_milvus_row("<span>X</span>"))
    assert fixed is not None
    assert "id" not in fixed


def test_corrected_row_keeps_vector_and_content_identical() -> None:
    """Byte-identical reinsert: vector/content are the same objects queried."""
    row = _milvus_row("<span>X</span>")
    fixed = corrected_milvus_row(row)
    assert fixed is not None
    assert fixed["vector"] is row["vector"]
    assert fixed["content_chunk"] is row["content_chunk"]
    assert fixed["book_id"] is row["book_id"]


def test_corrected_row_preserves_unknown_metadata_keys() -> None:
    """Future metadata keys must survive the rewrite verbatim."""
    row = _milvus_row("<span>X</span>")
    metadata = row["metadata"]
    assert isinstance(metadata, dict)
    metadata["page"] = 12  # hypothetical future key
    fixed = corrected_milvus_row(row)
    assert fixed is not None
    fixed_metadata = fixed["metadata"]
    assert isinstance(fixed_metadata, dict)
    assert fixed_metadata["page"] == 12


def test_corrected_row_does_not_mutate_input() -> None:
    row = _milvus_row("<span>X</span>")
    corrected_milvus_row(row)
    metadata = row["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["parent_section"] == "<span>X</span>"


def test_corrected_row_returns_none_when_clean() -> None:
    assert corrected_milvus_row(_milvus_row("Chapter 1")) is None
    assert corrected_milvus_row(_milvus_row(None)) is None


def test_corrected_row_empty_after_strip_stores_none() -> None:
    fixed = corrected_milvus_row(_milvus_row('<a href="x.html"><span'))
    assert fixed is not None
    metadata = fixed["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["parent_section"] is None


def test_corrected_row_tolerates_malformed_metadata() -> None:
    """Non-dict metadata is reported upstream, never crashed on here."""
    row = _milvus_row("ignored")
    row["metadata"] = "not-a-dict"
    assert corrected_milvus_row(row) is None
    row["metadata"] = None
    assert corrected_milvus_row(row) is None


# ---------------------------------------------------------------------------
# sweep_orphans — book_id_expr allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "book_id",
    [
        "811f3136-b9bf-4ea3-a4e4-9687e3d26c60",  # canonical UUID
        "b_phase6_real_epub",  # legacy dev label (live debris)
        "cc7be4f5-c10c-4cb5-bc83-a3424db6953f",
    ],
)
def test_book_id_expr_accepts_legitimate_ids(book_id: str) -> None:
    assert book_id_expr(book_id) == f'book_id == "{book_id}"'


@pytest.mark.parametrize(
    "book_id",
    [
        "",
        'x" || book_id != "',  # expr injection
        'quote"inside',
        "back\\slash",
        "has space",
        "semi;colon",
        "new\nline",
        "x" * 65,  # over the VARCHAR(64) bound
    ],
)
def test_book_id_expr_refuses_unsafe_ids(book_id: str) -> None:
    """Anything outside the allowlist is refused, never escaped-and-hoped."""
    with pytest.raises(ValueError, match="expr-safety allowlist"):
        book_id_expr(book_id)


# ---------------------------------------------------------------------------
# sweep_orphans — classify
# ---------------------------------------------------------------------------


def _facts(
    book_id: str = "b",
    *,
    in_global_books: bool = False,
    chunk_count: int = 0,
    library_ref_count: int = 0,
    vector_count: int = 0,
    claimed: bool = False,
) -> BookFacts:
    return BookFacts(
        book_id=book_id,
        in_global_books=in_global_books,
        chunk_count=chunk_count,
        library_ref_count=library_ref_count,
        vector_count=vector_count,
        claimed=claimed,
    )


def test_classify_milvus_only_orphan() -> None:
    fact = _facts("b_phase6_real_epub", vector_count=167)
    plan = classify([fact])
    assert plan.milvus_orphans == (fact,)
    assert plan.pg_orphans == ()
    assert plan.refusals == ()
    assert plan.skipped_claims == ()


def test_classify_empty_global_books_row() -> None:
    fact = _facts("g1", in_global_books=True)
    plan = classify([fact])
    assert plan.pg_orphans == (fact,)
    assert plan.milvus_orphans == ()


def test_classify_live_book_untouched() -> None:
    """A healthy book — refs, chunks, vectors — is not candidate-shaped."""
    plan = classify(
        [
            _facts(
                "live",
                in_global_books=True,
                chunk_count=167,
                library_ref_count=1,
                vector_count=167,
            ),
        ],
    )
    assert plan == classify([])


def test_classify_book_with_chunks_never_candidate() -> None:
    """Chunks but no refs (and no vectors) is still not sweepable."""
    plan = classify([_facts("c", in_global_books=True, chunk_count=10)])
    assert plan == classify([])


def test_classify_mid_ingest_vectors_with_gb_row_untouched() -> None:
    """global_books row + vectors but zero chunks: not class (b) — left alone."""
    plan = classify([_facts("m", in_global_books=True, vector_count=42)])
    assert plan == classify([])


def test_classify_tenant_reachable_pg_candidate_is_refusal() -> None:
    """Candidate-shaped but referenced from user_library → hard refusal."""
    fact = _facts("r1", in_global_books=True, library_ref_count=1)
    plan = classify([fact])
    assert plan.refusals == (fact,)
    assert plan.milvus_orphans == ()
    assert plan.pg_orphans == ()


def test_classify_tenant_reachable_milvus_candidate_is_refusal() -> None:
    """FK-impossible today, but defense in depth: refs always trump sweeping."""
    fact = _facts("r2", vector_count=5, library_ref_count=2)
    plan = classify([fact])
    assert plan.refusals == (fact,)
    assert plan.milvus_orphans == ()


def test_classify_claimed_candidates_are_skipped() -> None:
    """An in-flight ingest claim (Phase 20 window) parks the candidate."""
    milvus_side = _facts("in_flight", vector_count=12, claimed=True)
    pg_side = _facts("pg_claim", in_global_books=True, claimed=True)
    plan = classify([milvus_side, pg_side])
    assert plan.skipped_claims == (milvus_side, pg_side)
    assert plan.milvus_orphans == ()
    assert plan.pg_orphans == ()


def test_classify_refusal_outranks_claim() -> None:
    """A claimed candidate that is also tenant-reachable still aborts the run."""
    fact = _facts("both", vector_count=3, library_ref_count=1, claimed=True)
    plan = classify([fact])
    assert plan.refusals == (fact,)
    assert plan.skipped_claims == ()


def test_classify_mixed_inventory_matches_live_dev_shape() -> None:
    """The actual Phase 21 dev inventory: 3 Milvus orphans + 5 live books."""
    live = [
        _facts(
            f"live-{i}",
            in_global_books=True,
            chunk_count=n,
            library_ref_count=1,
            vector_count=n,
        )
        for i, n in enumerate([208, 209, 603, 167, 27])
    ]
    orphans = [
        _facts("cc7be4f5-c10c-4cb5-bc83-a3424db6953f", vector_count=167),
        _facts("b_phase6_real_epub", vector_count=167),
        _facts("03691dda-fe24-4f87-ade0-8b1ccca36843", vector_count=27),
    ]
    plan = classify(live + orphans)
    assert plan.milvus_orphans == tuple(orphans)
    assert plan.pg_orphans == ()
    assert plan.refusals == ()
    assert plan.skipped_claims == ()
