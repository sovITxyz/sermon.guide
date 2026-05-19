"""Unit tests for search helpers — no DB, no Milvus, no model load.

Live retrieval is covered by ``worker/tests/test_retrieval_golden.py``;
this file pins the small, deterministic pieces of ``api/search.py`` so
regressions in filter-expression shape or short-circuit semantics surface
without requiring infra.
"""

# Tests exercise module-internals on purpose.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import uuid

import pytest

from search import _build_filter_expr


def test_build_filter_expr_quotes_each_uuid() -> None:
    a = uuid.UUID("11111111-1111-1111-1111-111111111111")
    b = uuid.UUID("22222222-2222-2222-2222-222222222222")
    expr = _build_filter_expr([a, b])
    # Milvus expects `book_id in ["uuid1", "uuid2"]`; the exact string is
    # the contract between the API and the partition-key filter.
    expected = (
        'book_id in ["11111111-1111-1111-1111-111111111111", '
        '"22222222-2222-2222-2222-222222222222"]'
    )
    assert expr == expected


def test_build_filter_expr_rejects_empty() -> None:
    # An empty filter list would either become `book_id in []` (which some
    # pymilvus builds reject) or — worse — an accidentally-unfiltered
    # search if the caller stripped the clause. The endpoint short-circuits
    # the request before calling this helper; we enforce that contract
    # here so a future caller can't bypass it silently.
    with pytest.raises(ValueError, match="at least one book_id"):
        _build_filter_expr([])


def test_build_filter_expr_single_book_id() -> None:
    bid = uuid.UUID("33333333-3333-3333-3333-333333333333")
    assert _build_filter_expr([bid]) == 'book_id in ["33333333-3333-3333-3333-333333333333"]'
