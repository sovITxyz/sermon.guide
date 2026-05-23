"""Unit tests for the Gemini summary agent glue (Phase 14).

Live Gemini calls + live retrieval are out of scope here — they need a
GOOGLE_API_KEY, a populated Milvus/Postgres, and ~30 s of CPU rerank (see the
Phase 14 verify notes in docs/PHASES.md). This file pins the deterministic
glue with every I/O seam monkeypatched: no network, no key, no DB, no model
load.

- Citation labels are marker-safe (`:` / `[` / `]` stripped), collision-de-
  duped, and fall back title → filename stem → book-id slug.
- `_build_sources` preserves rerank order and reads chunk_index from metadata.
- `_build_prompt` carries the query + every marker-prefixed passage.
- `_extract_citations` returns only markers present in the summary, in
  first-appearance order, and never an invented one — markers are
  bracket-delimited, so `[X:1]` cannot match inside `[X:12]`.
- `_generate_summary` wires the grounding config + prompt and fails loud (502)
  on an upstream error or empty candidate.
- The handler forces the full pipeline (`do_rerank=True`) with the JWT
  user_id, 503s when GOOGLE_API_KEY is unset *before* retrieval, and returns
  the deterministic no-context message (no LLM call) when nothing is retrieved.
"""

# Tests reach into module internals on purpose and pass duck-typed fakes where
# the handler annotates concrete DI types. Silence the wide-stub noise per-file
# like the sibling suites do.
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import HTTPException
from google.genai import errors
from pydantic import ValidationError

import summary as summary_module
from search import SearchHit

# --- fakes -----------------------------------------------------------------


class _FakeResponse:
    def __init__(self, text: str | None) -> None:
        self.text = text


class _FakeModels:
    """Stand-in for ``client.models``; records calls, returns / raises on demand."""

    def __init__(
        self,
        text: str | None = "A grounded answer.",
        raise_exc: Exception | None = None,
    ) -> None:
        self._text = text
        self._raise_exc = raise_exc
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, *, model: str, contents: str, config: Any) -> _FakeResponse:  # noqa: ANN401
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self._raise_exc is not None:
            raise self._raise_exc
        return _FakeResponse(self._text)


class _FakeClient:
    def __init__(
        self,
        text: str | None = "A grounded answer.",
        raise_exc: Exception | None = None,
    ) -> None:
        self.models = _FakeModels(text, raise_exc)


class _FakeUser:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()


def _hit(
    book_int: int,
    chunk_index: int,
    *,
    content: str = "passage",
    filename: str | None = "book.epub",
    parent_section: str | None = "ch1",
) -> SearchHit:
    metadata: dict[str, Any] = {"chunk_index": chunk_index}
    if filename is not None:
        metadata["filename"] = filename
    if parent_section is not None:
        metadata["parent_section"] = parent_section
    return SearchHit(
        book_id=uuid.UUID(int=book_int),
        content_chunk=content,
        metadata=metadata,
        score=1.0,
    )


# --- request schema --------------------------------------------------------


def test_request_defaults_and_bounds() -> None:
    assert summary_module.SummaryRequest(query="q").limit_chunks == 20
    with pytest.raises(ValidationError):
        summary_module.SummaryRequest(query="")
    with pytest.raises(ValidationError):
        summary_module.SummaryRequest(query="q", limit_chunks=0)


# --- citation labels -------------------------------------------------------


def test_label_strips_marker_breaking_chars() -> None:
    label = summary_module._label_from_title("Mere: [Christianity]", None, uuid.UUID(int=1))
    assert ":" not in label
    assert "[" not in label
    assert "]" not in label
    assert label == "Mere Christianity"


def test_label_collapses_whitespace_and_truncates() -> None:
    label = summary_module._label_from_title("Word " * 50, None, uuid.UUID(int=1))
    assert len(label) <= summary_module._MAX_LABEL_LEN
    assert "  " not in label


def test_label_falls_back_to_filename_stem() -> None:
    label = summary_module._label_from_title("", "/path/to/institutes.epub", uuid.UUID(int=1))
    assert label == "institutes"


def test_label_falls_back_to_book_slug() -> None:
    bid = uuid.uuid4()
    label = summary_module._label_from_title("", None, bid)
    assert label == f"book-{bid!s:.8}"
    assert ":" not in label


def test_assign_labels_distinct_books() -> None:
    b1, b2 = uuid.UUID(int=1), uuid.UUID(int=2)
    labels = summary_module._assign_book_labels(
        [_hit(1, 0), _hit(2, 0)],
        {b1: "Faith", b2: "Hope"},
    )
    assert labels == {b1: "Faith", b2: "Hope"}


def test_assign_labels_decollides_same_title() -> None:
    b1, b2 = uuid.UUID(int=1), uuid.UUID(int=2)
    labels = summary_module._assign_book_labels(
        [_hit(1, 0), _hit(2, 0)],
        {b1: "Institutes", b2: "Institutes"},
    )
    assert labels[b1] == "Institutes"
    assert labels[b2] == "Institutes (2)"


# --- source building -------------------------------------------------------


def test_build_sources_marker_and_order() -> None:
    b1, b2 = uuid.UUID(int=1), uuid.UUID(int=2)
    sources = summary_module._build_sources(
        [_hit(1, 5, content="c-a"), _hit(2, 9, content="c-b")],
        {b1: "Faith", b2: "Hope"},
    )
    assert [s.marker for s in sources] == ["[Faith:5]", "[Hope:9]"]
    assert [s.content for s in sources] == ["c-a", "c-b"]
    assert sources[0].chunk_index == 5
    assert sources[0].title == "Faith"


def test_build_sources_title_falls_back_to_label_when_missing() -> None:
    # No title row → label derives from the filename stem, and the citation
    # title falls back to that same label rather than being empty.
    sources = summary_module._build_sources([_hit(1, 0, filename="grace.epub")], {})
    assert sources[0].marker == "[grace:0]"
    assert sources[0].title == "grace"


def test_build_sources_non_str_metadata_becomes_none() -> None:
    b1 = uuid.UUID(int=1)
    hit = SearchHit(
        book_id=b1,
        content_chunk="x",
        metadata={"chunk_index": 2, "filename": 123, "parent_section": None},
        score=1.0,
    )
    sources = summary_module._build_sources([hit], {b1: "Title"})
    assert sources[0].filename is None
    assert sources[0].parent_section is None


# --- prompt assembly -------------------------------------------------------


def test_build_prompt_includes_query_and_markers() -> None:
    b1 = uuid.UUID(int=1)
    sources = summary_module._build_sources([_hit(1, 3, content="grace abounds")], {b1: "Romans"})
    prompt = summary_module._build_prompt("what about grace?", sources)
    assert "what about grace?" in prompt
    assert "[Romans:3]" in prompt
    assert "grace abounds" in prompt


# --- citation extraction ---------------------------------------------------


def test_extract_citations_returns_only_present_markers() -> None:
    b1, b2 = uuid.UUID(int=1), uuid.UUID(int=2)
    sources = summary_module._build_sources([_hit(1, 0), _hit(2, 0)], {b1: "Faith", b2: "Hope"})
    cites = summary_module._extract_citations("Faith matters [Faith:0]. Hope unused.", sources)
    assert [c.marker for c in cites] == ["[Faith:0]"]
    assert cites[0].book_id == b1


def test_extract_citations_first_appearance_order() -> None:
    b1, b2 = uuid.UUID(int=1), uuid.UUID(int=2)
    sources = summary_module._build_sources([_hit(1, 0), _hit(2, 0)], {b1: "Faith", b2: "Hope"})
    cites = summary_module._extract_citations("First [Hope:0] then [Faith:0].", sources)
    assert [c.marker for c in cites] == ["[Hope:0]", "[Faith:0]"]


def test_extract_citations_ignores_unknown_markers() -> None:
    b1 = uuid.UUID(int=1)
    sources = summary_module._build_sources([_hit(1, 0)], {b1: "Faith"})
    assert summary_module._extract_citations("Invented [Nonexistent:7] reference.", sources) == []


def test_extract_citations_substring_safety() -> None:
    # `[Faith:1]` must not resolve inside `[Faith:12]` — the closing bracket
    # delimits the marker.
    b1 = uuid.UUID(int=1)
    sources = summary_module._build_sources([_hit(1, 1), _hit(1, 12)], {b1: "Faith"})
    cites = summary_module._extract_citations("See [Faith:12] only.", sources)
    assert [c.marker for c in cites] == ["[Faith:12]"]


# --- gemini call -----------------------------------------------------------


def test_generate_summary_wires_config_and_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(text="  A grounded answer [Faith:0].  ")
    monkeypatch.setattr(summary_module, "_client", lambda: fake)
    sources = summary_module._build_sources(
        [_hit(1, 0, content="grace")],
        {uuid.UUID(int=1): "Faith"},
    )

    out = summary_module._generate_summary(query="q?", sources=sources)

    assert out == "A grounded answer [Faith:0]."  # stripped
    call = fake.models.calls[0]
    assert call["model"] == summary_module.GEMINI_MODEL
    assert call["config"].system_instruction == summary_module._SYSTEM_INSTRUCTION
    assert call["config"].temperature == summary_module._TEMPERATURE
    assert call["config"].max_output_tokens == summary_module._MAX_OUTPUT_TOKENS
    assert "[Faith:0]" in call["contents"]
    assert "q?" in call["contents"]


def test_generate_summary_raises_502_on_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    exc = errors.APIError(503, {"error": {"message": "down", "status": "UNAVAILABLE"}})
    fake = _FakeClient(raise_exc=exc)
    monkeypatch.setattr(summary_module, "_client", lambda: fake)
    sources = summary_module._build_sources([_hit(1, 0)], {uuid.UUID(int=1): "Faith"})

    with pytest.raises(HTTPException) as excinfo:
        summary_module._generate_summary(query="q", sources=sources)
    assert excinfo.value.status_code == 502


@pytest.mark.parametrize("empty", [None, "   "])
def test_generate_summary_raises_502_on_empty_text(
    monkeypatch: pytest.MonkeyPatch,
    empty: str | None,
) -> None:
    fake = _FakeClient(text=empty)
    monkeypatch.setattr(summary_module, "_client", lambda: fake)
    sources = summary_module._build_sources([_hit(1, 0)], {uuid.UUID(int=1): "Faith"})

    with pytest.raises(HTTPException) as excinfo:
        summary_module._generate_summary(query="q", sources=sources)
    assert excinfo.value.status_code == 502


# --- handler ---------------------------------------------------------------


async def test_handler_503_when_key_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing key → 503 *before* retrieval (don't burn ~30s of CPU rerank)."""
    monkeypatch.setattr(summary_module.settings, "google_api_key", None)
    called = {"run_search": False}

    async def _fake_run_search(**_: Any) -> list[SearchHit]:  # noqa: ANN401
        called["run_search"] = True
        return []

    monkeypatch.setattr(summary_module, "run_search", _fake_run_search)
    user: Any = _FakeUser()
    session: Any = object()

    with pytest.raises(HTTPException) as excinfo:
        await summary_module.search_summary(
            payload=summary_module.SummaryRequest(query="q"),
            current_user=user,
            session=session,
        )
    assert excinfo.value.status_code == 503
    assert called["run_search"] is False


async def test_handler_no_context_message_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summary_module.settings, "google_api_key", "k")
    gen_called = {"x": False}

    async def _fake_run_search(**_: Any) -> list[SearchHit]:  # noqa: ANN401
        return []

    def _fake_gen(**_: Any) -> str:  # noqa: ANN401
        gen_called["x"] = True
        return "should not be reached"

    monkeypatch.setattr(summary_module, "run_search", _fake_run_search)
    monkeypatch.setattr(summary_module, "_generate_summary", _fake_gen)
    user: Any = _FakeUser()
    session: Any = object()

    resp = await summary_module.search_summary(
        payload=summary_module.SummaryRequest(query="q"),
        current_user=user,
        session=session,
    )
    assert resp.summary == summary_module._NO_CONTEXT_MESSAGE
    assert resp.citations == []
    assert gen_called["x"] is False


async def test_handler_happy_path_forces_rerank_and_extracts_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(summary_module.settings, "google_api_key", "k")
    user = _FakeUser()
    b1 = uuid.UUID(int=1)
    recorded: dict[str, Any] = {}

    async def _fake_run_search(
        *,
        query: str,
        limit: int,
        do_rerank: bool,
        user_id: uuid.UUID,
        session: Any,  # noqa: ANN401
    ) -> list[SearchHit]:
        recorded.update(
            query=query,
            limit=limit,
            do_rerank=do_rerank,
            user_id=user_id,
            session=session,
        )
        return [_hit(1, 7, content="grace abounds")]

    async def _fake_resolve_titles(_session: Any, _book_ids: Any) -> dict[uuid.UUID, str]:  # noqa: ANN401
        return {b1: "Romans"}

    monkeypatch.setattr(summary_module, "run_search", _fake_run_search)
    monkeypatch.setattr(summary_module, "_resolve_titles", _fake_resolve_titles)
    monkeypatch.setattr(
        summary_module,
        "_client",
        lambda: _FakeClient(text="Grace is central [Romans:7]."),
    )

    user_arg: Any = user
    session_arg: Any = object()
    resp = await summary_module.search_summary(
        payload=summary_module.SummaryRequest(query="grace?", limit_chunks=12),
        current_user=user_arg,
        session=session_arg,
    )

    # Tenant-critical: the summary endpoint forces the full rerank pipeline and
    # passes the JWT-derived user_id straight through to run_search.
    assert recorded["do_rerank"] is True
    assert recorded["user_id"] == user.user_id
    assert recorded["limit"] == 12
    assert resp.summary == "Grace is central [Romans:7]."
    assert [c.marker for c in resp.citations] == ["[Romans:7]"]
    assert resp.citations[0].book_id == b1
    assert resp.citations[0].chunk_index == 7
    assert resp.citations[0].title == "Romans"
