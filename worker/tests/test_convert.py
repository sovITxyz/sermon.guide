"""DOCX round-trip phase gate (Phase 43).

The load-bearing check: a citation-bearing ProseMirror document survives
``convert_to_docx`` -> ``convert_from_docx`` with its STRUCTURE preserved and —
critically — every ``/read`` citation deep-link recovered (``bookId`` +
``chunkIndex``). ``data-*`` attrs do not survive ``.docx``; the citation node
round-trips as a hyperlink instead, and this test proves that hyperlink survives
pandoc both ways and is rebuilt into a citation node.

This suite is KEYLESS (no DeepInfra / Milvus / Postgres) but needs three host
deps: ``pandoc``, Node 22, and the populated ``convert_node`` ``node_modules``.
Mirroring the live-test skip discipline (test_extractors.py et al.), it SKIPS
cleanly when any of those is absent so CI's keyless worker job stays green —
but on a fully provisioned box (this one) it MUST run and pass.
"""

# pypandoc has no PEP 561 marker; only touched inside the skip-guard.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

_WORKER_ROOT = Path(__file__).resolve().parent.parent
_NODE_CLI = _WORKER_ROOT / "convert_node" / "cli.mjs"
_NODE_MODULES = _WORKER_ROOT / "convert_node" / "node_modules"
_REFERENCE_DOCX = _WORKER_ROOT / "assets" / "reference.docx"


def _pandoc_available() -> bool:
    try:
        import pypandoc
    except ImportError:
        return False
    try:
        pypandoc.get_pandoc_version()
    except OSError:
        return False
    return True


def _node_bundle_available() -> bool:
    """True iff `node` is on PATH and the convert_node bundle is installed."""
    return (
        shutil.which("node") is not None
        and _NODE_CLI.exists()
        and _NODE_MODULES.is_dir()
        and _REFERENCE_DOCX.exists()
    )


# One reason string covering every missing-host-dep case (pandoc OR node OR the
# bundle OR the reference template) — the suite skips, never fails, when the box
# is not provisioned for the round-trip.
_SKIP_REASON = (
    "docx round-trip needs pandoc + Node 22 + a populated worker/convert_node/"
    "node_modules (npm install) + assets/reference.docx"
)

pytestmark = pytest.mark.skipif(
    not (_pandoc_available() and _node_bundle_available()),
    reason=_SKIP_REASON,
)


# A citation-bearing manuscript: heading + paragraph (with a bold mark) +
# two citations (distinct bookId/chunkIndex, one with an encode-sensitive id) +
# a bullet list. Mirrors the editor's StarterKit + citation schema.
_DOC: dict[str, Any] = {
    "type": "doc",
    "content": [
        {
            "type": "heading",
            "attrs": {"level": 2},
            "content": [{"type": "text", "text": "On Grace"}],
        },
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Grace is "},
                {"type": "text", "marks": [{"type": "bold"}], "text": "unmerited"},
                {"type": "text", "text": " favor."},
            ],
        },
        {
            "type": "citation",
            "attrs": {
                "bookId": "11111111-2222-3333-4444-555555555555",
                "chunkIndex": 42,
                "bookTitle": "All of Grace",
                "snippet": "A cached snippet that the docx cannot carry.",
                "parentSection": "Chapter 1",
            },
        },
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": "A second witness:"}],
        },
        {
            "type": "citation",
            "attrs": {
                "bookId": "book-with-dashes-2",
                "chunkIndex": 7,
                "bookTitle": "Holiness",
                "snippet": "",
                "parentSection": None,
            },
        },
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "First point"}],
                        }
                    ],
                },
                {
                    "type": "listItem",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "Second point"}],
                        }
                    ],
                },
            ],
        },
    ],
}


def _walk(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Depth-first walk over a ProseMirror node tree, yielding every node."""
    yield node
    content: object = node.get("content")
    if not isinstance(content, list):
        return
    for child in content:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(child, dict):
            yield from _walk(cast("dict[str, Any]", child))


def _node_types(doc: dict[str, Any]) -> set[str]:
    return {n["type"] for n in _walk(doc) if isinstance(n.get("type"), str)}


def _citations(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [n for n in _walk(doc) if n.get("type") == "citation"]


def _all_text(doc: dict[str, Any]) -> str:
    return " ".join(n["text"] for n in _walk(doc) if n.get("type") == "text")


def _is_bold_mark(mark: object) -> bool:
    return isinstance(mark, dict) and cast("dict[str, Any]", mark).get("type") == "bold"


def test_docx_round_trip_preserves_structure_and_citations() -> None:
    """JSON -> docx -> JSON keeps the structure and every /read citation link."""
    from convert import convert_from_docx, convert_to_docx

    docx_bytes = convert_to_docx(_DOC)
    # A real OOXML file (PK zip magic), non-trivial size.
    assert docx_bytes[:2] == b"PK", "output is not a docx (zip) container"
    assert len(docx_bytes) > 1000

    restored = convert_from_docx(docx_bytes)
    assert restored.get("type") == "doc"

    # Structure: the headings/paragraphs/lists/citations all survive.
    types = _node_types(restored)
    for expected in ("heading", "paragraph", "citation", "bulletList", "listItem"):
        assert expected in types, f"{expected} node lost in round-trip; got {sorted(types)}"

    # The heading level survives.
    headings = [n for n in _walk(restored) if n.get("type") == "heading"]
    assert any(h.get("attrs", {}).get("level") == 2 for h in headings)

    # The bold mark survives somewhere in the text.
    def _has_bold(node: dict[str, Any]) -> bool:
        marks: object = node.get("marks")
        if not isinstance(marks, list):
            return False
        marks_list = cast("list[object]", marks)
        return any(_is_bold_mark(m) for m in marks_list)

    marked = [n for n in _walk(restored) if n.get("type") == "text" and _has_bold(n)]
    assert marked, "bold mark lost in round-trip"

    # Body text survives.
    text = _all_text(restored)
    assert "unmerited" in text
    assert "First point" in text
    assert "Second point" in text

    # THE GATE: both citations come back, deep-link intact (bookId + chunkIndex
    # recovered from the /read hyperlink). bookTitle is degraded-from-anchor-text
    # (best-effort); snippet/parentSection are not carried by docx.
    restored_cites = _citations(restored)
    assert len(restored_cites) == 2, (
        f"expected 2 citations, got {len(restored_cites)}: {restored_cites}"
    )
    by_book = {c["attrs"]["bookId"]: c["attrs"] for c in restored_cites}

    assert "11111111-2222-3333-4444-555555555555" in by_book
    assert by_book["11111111-2222-3333-4444-555555555555"]["chunkIndex"] == 42
    # Degraded title recovered from the anchor text.
    assert by_book["11111111-2222-3333-4444-555555555555"]["bookTitle"] == "All of Grace"

    assert "book-with-dashes-2" in by_book
    assert by_book["book-with-dashes-2"]["chunkIndex"] == 7
    assert by_book["book-with-dashes-2"]["bookTitle"] == "Holiness"


def test_import_strips_dangerous_and_external_links() -> None:
    """A hostile docx-derived HTML must not mint citations from unsafe hrefs.

    The import leg only rebuilds a citation from a SAME-ORIGIN ``/read/<id>``
    path. ``javascript:``/``data:`` and absolute/external links are not reader
    deep-links, so they never become citation nodes (and with StarterKit's Link
    disabled they carry no clickable href into the stored JSON either).
    """
    from convert import convert_from_docx, convert_to_docx

    hostile = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "javascript:alert(1) and //evil.test/x"}],
            },
            {
                "type": "citation",
                "attrs": {
                    "bookId": "safe-book",
                    "chunkIndex": 3,
                    "bookTitle": "Safe",
                    "snippet": "",
                    "parentSection": None,
                },
            },
        ],
    }
    restored = convert_from_docx(convert_to_docx(hostile))

    cites = _citations(restored)
    # Only the genuine /read citation survives as a citation node.
    assert len(cites) == 1
    assert cites[0]["attrs"]["bookId"] == "safe-book"
    assert cites[0]["attrs"]["chunkIndex"] == 3

    # No citation was minted from the dangerous-scheme text.
    for c in cites:
        book_id = c["attrs"]["bookId"]
        assert "javascript" not in book_id
        assert "evil" not in book_id
