"""Unit tests for the MinHash LSH dedup module.

Three layers:

1. **Signature math** — exact-text round-trip, near-duplicate detection
   above the 0.85 threshold, dissimilar-text rejection. Pure
   ``signature()`` + ``MinHash.jaccard``; no LSH state.
2. **Serialize round-trip** — ``serialize`` then ``deserialize`` reproduces
   the same Jaccard.
3. **Dedup index behaviour** — ``find_duplicate`` returns ``None`` on an
   empty index, returns the seeded ``book_id`` for an exact match, and
   rejects looser-than-construction thresholds.

The tests do not stand up Postgres. ``Dedup._load_from`` accepts an
explicit row iterable so the LSH can be seeded without DB I/O — see the
test seam comment on ``Dedup._load_from``.

NLTK's WordNet corpus is required for ``signature()``. The fixtures skip
when it isn't available locally, matching the BGE-Large cache-presence
gate in ``test_ingest.py``.
"""
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportPrivateUsage=false

from __future__ import annotations

import uuid

import pytest

from dedup import (
    DEFAULT_THRESHOLD,
    NUM_PERM,
    Dedup,
    deserialize,
    serialize,
    signature,
)


def _wordnet_available() -> bool:
    try:
        import nltk

        nltk.data.find("corpora/wordnet")
    except (ImportError, LookupError):
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _wordnet_available(),
    reason="NLTK WordNet corpus not installed; run `nltk.download('wordnet')` to enable.",
)


# Long enough for SemanticSplitter's threshold + plenty of 5-shingles for
# MinHash signal. Sourced from public-domain (Lincoln's Gettysburg Address).
LINCOLN = (
    "Four score and seven years ago our fathers brought forth on this "
    "continent, a new nation, conceived in Liberty, and dedicated to the "
    "proposition that all men are created equal. Now we are engaged in a "
    "great civil war, testing whether that nation, or any nation so "
    "conceived and so dedicated, can long endure. We are met on a great "
    "battle-field of that war. We have come to dedicate a portion of that "
    "field, as a final resting place for those who here gave their lives "
    "that that nation might live. It is altogether fitting and proper that "
    "we should do this."
)

# Same content with minor edits — different edition / OCR drift simulation.
LINCOLN_EDITED = LINCOLN.replace("Liberty", "liberty").replace("battle-field", "battlefield")

# Unrelated public-domain text (start of Pride and Prejudice).
AUSTEN = (
    "It is a truth universally acknowledged, that a single man in "
    "possession of a good fortune, must be in want of a wife. However "
    "little known the feelings or views of such a man may be on his first "
    "entering a neighbourhood, this truth is so well fixed in the minds of "
    "the surrounding families, that he is considered the rightful property "
    "of some one or other of their daughters."
)


class TestSignature:
    """``signature()`` produces MinHashes whose Jaccard tracks textual similarity.

    If these fail the dedup gate is either falsely matching (would
    silently share vectors across unrelated books) or falsely missing
    (would re-embed near-duplicates and burn the projected 80% storage
    savings).
    """

    def test_identical_text_jaccards_to_one(self) -> None:
        sig_a = signature(LINCOLN)
        sig_b = signature(LINCOLN)
        assert sig_a.jaccard(sig_b) == 1.0

    def test_near_duplicate_above_threshold(self) -> None:
        sig_a = signature(LINCOLN)
        sig_b = signature(LINCOLN_EDITED)
        assert sig_a.jaccard(sig_b) >= DEFAULT_THRESHOLD, (
            f"Near-duplicate Jaccard {sig_a.jaccard(sig_b):.3f} fell "
            f"below the {DEFAULT_THRESHOLD} dedup threshold — different "
            "editions would silently re-embed."
        )

    def test_dissimilar_text_below_threshold(self) -> None:
        sig_a = signature(LINCOLN)
        sig_b = signature(AUSTEN)
        assert sig_a.jaccard(sig_b) < DEFAULT_THRESHOLD, (
            f"Unrelated texts Jaccarded to {sig_a.jaccard(sig_b):.3f}, "
            f"≥ {DEFAULT_THRESHOLD} — dedup would falsely share vectors."
        )

    def test_short_input_returns_empty_signature(self) -> None:
        """Under five tokens → no shingles → empty MinHash; jaccard against
        a populated signature is 0.0, so the document never matches a
        duplicate even spuriously.
        """
        sig = signature("hi")
        populated = signature(LINCOLN)
        assert sig.jaccard(populated) == 0.0


class TestSerialize:
    def test_round_trip_preserves_jaccard(self) -> None:
        sig = signature(LINCOLN)
        raw = serialize(sig)
        restored = deserialize(raw)
        assert sig.jaccard(restored) == 1.0

    def test_serialized_size_matches_num_perm(self) -> None:
        """64-bit hashvalues × NUM_PERM = expected byte length.

        Catches drift in the (num_perm, dtype) contract — any change
        breaks every persisted signature in ``global_books``.
        """
        sig = signature(LINCOLN)
        assert len(serialize(sig)) == NUM_PERM * 8


class TestDedupIndex:
    """``Dedup.find_duplicate`` round-trips ``Dedup.add``.

    Uses ``_load_from`` directly so no Postgres is required. The DB-driven
    rehydration path is exercised manually for Phase 8 acceptance.
    """

    def _empty_index(self) -> Dedup:
        # session_factory is never invoked because we pre-seed via
        # _load_from. Pass a placeholder typed cast for the test.
        d = Dedup(session_factory=None)  # type: ignore[arg-type]
        d._load_from([])
        return d

    def test_find_returns_none_on_empty_index(self) -> None:
        d = self._empty_index()
        assert d.find_duplicate(signature(LINCOLN)) is None

    def test_add_then_find_returns_seeded_book_id(self) -> None:
        d = self._empty_index()
        book_id = uuid.uuid4()
        d.add(book_id, signature(LINCOLN))

        assert d.find_duplicate(signature(LINCOLN)) == book_id

    def test_near_duplicate_match(self) -> None:
        d = self._empty_index()
        book_id = uuid.uuid4()
        d.add(book_id, signature(LINCOLN))

        assert d.find_duplicate(signature(LINCOLN_EDITED)) == book_id

    def test_unrelated_text_no_match(self) -> None:
        d = self._empty_index()
        d.add(uuid.uuid4(), signature(LINCOLN))

        assert d.find_duplicate(signature(AUSTEN)) is None

    def test_load_from_rehydrates_via_serialized_bytes(self) -> None:
        """The DB-rehydration path: feed ``(book_id, bytes)`` rows through
        ``_load_from`` exactly like a worker-start cold load would.
        """
        seeded_id = uuid.uuid4()
        seeded_sig_bytes = serialize(signature(LINCOLN))

        d = Dedup(session_factory=None)  # type: ignore[arg-type]
        d._load_from([(seeded_id, seeded_sig_bytes)])

        assert d.find_duplicate(signature(LINCOLN)) == seeded_id

    def test_rejects_threshold_looser_than_construction(self) -> None:
        d = self._empty_index()
        with pytest.raises(ValueError, match="looser than the LSH"):
            d.find_duplicate(signature(LINCOLN), threshold=0.5)
