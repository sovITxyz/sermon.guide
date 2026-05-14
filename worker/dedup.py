"""MinHash LSH dedup for the ingestion pipeline.

Catches near-duplicate book uploads — different editions, minor
formatting differences, OCR drift — so the embed step is skipped and a
new ``user_library`` row points at the existing ``global_books`` row
instead. ~80% storage savings projected at scale (ARCHITECTURE.md §2).

Pipeline contract (used by ``ingest.py``):

    sig = signature(markdown)
    match = dedup_index().find_duplicate(sig)   # uuid | None
    if match is None:
        # … chunk + embed + insert vectors + insert global_books with sig …
        dedup_index().add(new_book_id, sig)

``signature`` builds a MinHash over **lemmatized 5-shingles** of
``markdown``, matching the research-PDF dedup section (feature-vector
creation → hashing → similarity join → filtering) and
ARCHITECTURE.md §2.

The LSH itself lives in memory. Its inputs — one ``LargeBinary`` row
per ``global_books.book_id`` — are the persisted-in-Postgres half. On
first use within a worker process we rehydrate the in-memory LSH from
those rows (``Dedup._ensure_loaded``). ``Dedup.add`` keeps the
in-memory state in sync with subsequent ``global_books`` inserts.

Multi-process workers (Phase 9 Celery) get one ``Dedup`` per process via
``dedup_index()``; each load is cheap (≈ 1 KB per book) and the LSH
itself is read-mostly after warm-up.

CLI (run from ``worker/``):

    uv run python -c \
        "from dedup import signature; \
         print(len(signature('hello world phase eight dedup smoke').hashvalues))"
"""

# datasketch and nltk ship without PEP 561 stubs; relax the same strict
# rules other modules already relax for third-party untyped libs.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportUnknownLambdaType=false

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from functools import lru_cache
from typing import Final
from uuid import UUID

import numpy as np
from datasketch import MinHash, MinHashLSH
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from db import GlobalBook, get_sync_session_factory

NUM_PERM: Final = 128
SHINGLE_SIZE: Final = 5
SEED: Final = 1
DEFAULT_THRESHOLD: Final = 0.85

# Tokenizer: any run of ASCII letters + apostrophes. The corpus is English-
# first per ARCHITECTURE.md §2 (BGE-Large English retrieval embedder); CJK,
# accented Latin, and numbers are dropped rather than added as noise to the
# signature.
_WORD = re.compile(r"[A-Za-z']+")


@lru_cache(maxsize=1)
def _lemmatizer():
    """Lazily load NLTK's WordNet lemmatizer.

    First call after a cold env downloads the ~40MB ``wordnet`` corpus
    into ``~/nltk_data/``. Subsequent calls hit the local cache. CI and
    production workers should pre-warm by running ``signature("hello"*10)``
    once at image-build time so request-path latency doesn't carry the
    download.
    """
    import nltk
    from nltk.stem import WordNetLemmatizer

    try:
        nltk.data.find("corpora/wordnet")
    except LookupError:
        nltk.download("wordnet", quiet=True)
    return WordNetLemmatizer()


def _lemmas(text: str) -> list[str]:
    """Tokenize, lowercase, and lemmatize *text* (WordNet default POS='n')."""
    lemm = _lemmatizer()
    return [lemm.lemmatize(w) for w in _WORD.findall(text.lower())]


def signature(markdown: str) -> MinHash:
    """Compute a MinHash signature over lemmatized 5-shingles of *markdown*.

    Returns an empty MinHash (no shingles fed in) when fewer than five
    tokens are present — LSH lookup against an empty signature yields no
    candidates, the right behavior for a near-empty document.
    """
    mh = MinHash(num_perm=NUM_PERM, seed=SEED)
    lemmas = _lemmas(markdown)
    if len(lemmas) < SHINGLE_SIZE:
        return mh
    for i in range(len(lemmas) - SHINGLE_SIZE + 1):
        shingle = " ".join(lemmas[i : i + SHINGLE_SIZE])
        mh.update(shingle.encode("utf-8"))
    return mh


def serialize(sig: MinHash) -> bytes:
    """Serialize ``sig.hashvalues`` for the ``global_books.minhash_signature`` column.

    Only the hashvalues array is stored; reconstruction uses the
    ``(NUM_PERM, SEED)`` module constants — changing either invalidates
    every persisted signature.
    """
    return sig.hashvalues.tobytes()


def deserialize(data: bytes) -> MinHash:
    """Reconstruct a MinHash from ``serialize`` output."""
    mh = MinHash(num_perm=NUM_PERM, seed=SEED)
    mh.hashvalues = np.frombuffer(data, dtype=np.uint64).copy()
    return mh


class Dedup:
    """In-memory MinHash LSH index, lazy-loaded from ``global_books``.

    LSH bucket parameters are derived by datasketch from
    ``(num_perm, threshold)`` at construction time. ``find_duplicate``
    with a stricter threshold than construction post-filters candidates
    via real Jaccard; a looser threshold would silently miss candidates
    by design — rejected, the index must be rebuilt instead.

    ``find_duplicate`` and ``add`` are thread-safe enough for the
    single-worker Phase 8 path. Phase 9 Celery should keep one
    ``Dedup`` per process via ``dedup_index()``.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        self._sf = session_factory
        self._threshold = threshold
        self._lsh: MinHashLSH | None = None
        self._sigs: dict[UUID, MinHash] = {}
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> MinHashLSH:
        if self._lsh is not None:
            return self._lsh
        with self._lock:
            if self._lsh is None:
                self._load_from(self._fetch_rows())
        # _load_from always assigns to self._lsh; the lock guarantees we
        # see the completed write here.
        assert self._lsh is not None  # noqa: S101 — load invariant
        return self._lsh

    def _fetch_rows(self) -> list[tuple[UUID, bytes]]:
        """Read every ``(book_id, minhash_signature)`` pair from Postgres."""
        with self._sf() as session:
            return [
                (book_id, sig_bytes)
                for book_id, sig_bytes in session.execute(
                    select(GlobalBook.book_id, GlobalBook.minhash_signature),
                ).all()
            ]

    def _load_from(self, rows: Iterable[tuple[UUID, bytes]]) -> None:
        """Build the in-memory LSH + sig map from *rows*.

        Split from ``_fetch_rows`` so tests can pre-seed the index
        without standing up Postgres.
        """
        lsh = MinHashLSH(threshold=self._threshold, num_perm=NUM_PERM)
        sigs: dict[UUID, MinHash] = {}
        for book_id, sig_bytes in rows:
            sig = deserialize(sig_bytes)
            lsh.insert(str(book_id), sig)
            sigs[book_id] = sig
        self._lsh = lsh
        self._sigs = sigs

    def find_duplicate(
        self,
        sig: MinHash,
        *,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> UUID | None:
        """Return the ``GlobalBook.book_id`` whose signature ≥ *threshold* similar to *sig*.

        Loosening *threshold* below the LSH construction threshold would
        silently miss candidates (the buckets are sized for the
        construction threshold); reject so callers do not accidentally
        widen the dedup gate at query time.
        """
        if threshold < self._threshold:
            msg = (
                f"find_duplicate threshold={threshold} is looser than the "
                f"LSH construction threshold {self._threshold}; rebuild the "
                "index with the looser value instead of widening at query "
                "time."
            )
            raise ValueError(msg)

        lsh = self._ensure_loaded()
        # datasketch types LSH.query as list[Hashable]; we only ever
        # insert strs, so coerce here rather than threading Hashable
        # through every caller.
        candidates = [str(c) for c in lsh.query(sig)]
        if not candidates:
            return None
        if threshold == self._threshold:
            return UUID(candidates[0])

        best: tuple[float, UUID] | None = None
        for cand_str in candidates:
            cand_id = UUID(cand_str)
            cand_sig = self._sigs.get(cand_id)
            if cand_sig is None:
                continue
            j = sig.jaccard(cand_sig)
            if j >= threshold and (best is None or j > best[0]):
                best = (j, cand_id)
        return best[1] if best is not None else None

    def add(self, book_id: UUID, sig: MinHash) -> None:
        """Record a newly-inserted ``global_books`` row in the in-memory LSH."""
        lsh = self._ensure_loaded()
        with self._lock:
            lsh.insert(str(book_id), sig)
            self._sigs[book_id] = sig


@lru_cache(maxsize=1)
def dedup_index() -> Dedup:
    """Process-wide singleton ``Dedup`` against the shared sync session factory."""
    return Dedup(get_sync_session_factory())
