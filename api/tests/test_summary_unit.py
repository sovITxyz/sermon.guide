"""Unit tests for the LLM summary agent glue (Phase 14; transport re-cut in Phase 14b).

Live LLM calls + live retrieval are out of scope here — they need a real
provider key, a populated Milvus/Postgres, and ~30 s of CPU rerank (see the
Phase 14/14b verify notes in docs/PHASES.md). This file pins the deterministic
glue with every I/O seam monkeypatched: no network, no key, no DB, no model
load.

- Citation labels are marker-safe (`:` / `[` / `]` stripped), collision-de-
  duped, and fall back title → filename stem → book-id slug.
- `_build_sources` preserves rerank order and reads chunk_index from metadata.
- `_build_prompt` carries the query + every marker-prefixed passage.
- `_extract_citations` returns only markers present in the summary, in
  first-appearance order, and never an invented one — markers are
  bracket-delimited, so `[X:1]` cannot match inside `[X:12]`.
- `_generate_summary` wires the system+user messages, the sampling knobs, and
  the active provider's pinned model through `chat.completions.create`, and
  fails loud (502) on an upstream `openai.APIError`, an empty completion, or
  a choices-less response.
- Provider resolution (Phase 14b, ADR 0005): default is google; flipping to
  ppq picks the ppq base_url/model/key; SERMON_API_LLM_MODEL overrides the
  model id; the 503 guard names the ACTIVE provider's missing env var and
  never silently cross-pairs one provider with the other's key.
- The handler forces the full pipeline (`do_rerank=True`) with the JWT
  user_id, 503s when the active provider's key is unset *before* retrieval,
  and returns the deterministic no-context message (no LLM call) when nothing
  is retrieved.
"""

# Tests reach into module internals on purpose and pass duck-typed fakes where
# the handler annotates concrete DI types. Silence the wide-stub noise per-file
# like the sibling suites do.
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

import uuid
from typing import Any

import httpx
import openai
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import summary as summary_module
from search import SearchHit
from settings import ApiSettings

# --- fakes -----------------------------------------------------------------


class _FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str | None, *, no_choices: bool = False) -> None:
        self.choices: list[_FakeChoice] = [] if no_choices else [_FakeChoice(content)]


class _FakeCompletions:
    """Stand-in for ``client.chat.completions``; records calls, returns / raises on demand."""

    def __init__(
        self,
        text: str | None,
        raise_exc: Exception | None,
        *,
        no_choices: bool,
    ) -> None:
        self._text = text
        self._raise_exc = raise_exc
        self._no_choices = no_choices
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeResponse:  # noqa: ANN401
        self.calls.append(kwargs)
        if self._raise_exc is not None:
            raise self._raise_exc
        return _FakeResponse(self._text, no_choices=self._no_choices)


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeClient:
    def __init__(
        self,
        text: str | None = "A grounded answer.",
        raise_exc: Exception | None = None,
        *,
        no_choices: bool = False,
    ) -> None:
        self.chat = _FakeChat(_FakeCompletions(text, raise_exc, no_choices=no_choices))

    @property
    def calls(self) -> list[dict[str, Any]]:
        """Recorded ``chat.completions.create`` kwargs."""
        return self.chat.completions.calls


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
    # Phase 16: the citation carries the pruned chunk text so the UI can
    # render a preview without a second round-trip.
    assert cites[0].content == "passage"


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


# --- llm call ----------------------------------------------------------------


def test_generate_summary_wires_config_and_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summary_module.settings, "llm_provider", "google")
    monkeypatch.setattr(summary_module.settings, "llm_model", None)
    fake = _FakeClient(text="  A grounded answer [Faith:0].  ")
    monkeypatch.setattr(summary_module, "_client", lambda: fake)
    sources = summary_module._build_sources(
        [_hit(1, 0, content="grace")],
        {uuid.UUID(int=1): "Faith"},
    )

    out = summary_module._generate_summary(query="q?", sources=sources)

    assert out == "A grounded answer [Faith:0]."  # stripped
    call = fake.calls[0]
    # The pinned google-arm default (ADR 0005) — bare id, no "google/" prefix.
    assert call["model"] == "gemini-2.5-flash"
    assert call["temperature"] == summary_module._TEMPERATURE
    assert call["max_tokens"] == summary_module._MAX_OUTPUT_TOKENS
    system, user = call["messages"]
    assert system == {"role": "system", "content": summary_module._SYSTEM_INSTRUCTION}
    assert user["role"] == "user"
    assert "[Faith:0]" in user["content"]
    assert "q?" in user["content"]


def test_generate_summary_omits_reasoning_effort_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset SERMON_API_LLM_REASONING_EFFORT → nothing extra on the wire.

    Phase 16b: the knob must be opt-in — gateways that reject unknown
    params (or models that error on them) keep working untouched.
    """
    monkeypatch.setattr(summary_module.settings, "llm_reasoning_effort", None)
    fake = _FakeClient(text="ok")
    monkeypatch.setattr(summary_module, "_client", lambda: fake)
    sources = summary_module._build_sources([_hit(1, 0)], {uuid.UUID(int=1): "Faith"})

    summary_module._generate_summary(query="q", sources=sources)

    assert fake.calls[0]["extra_body"] is None


def test_generate_summary_sends_reasoning_effort_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SERMON_API_LLM_REASONING_EFFORT=none rides extra_body verbatim.

    Phase 16b latency lever — Google's OpenAI-compat layer accepts
    "none" to disable Gemini 2.5 Flash thinking (~60s → seconds).
    extra_body (not the SDK's typed param) so values the SDK literal
    hasn't caught up to still pass through.
    """
    monkeypatch.setattr(summary_module.settings, "llm_reasoning_effort", "none")
    fake = _FakeClient(text="ok")
    monkeypatch.setattr(summary_module, "_client", lambda: fake)
    sources = summary_module._build_sources([_hit(1, 0)], {uuid.UUID(int=1): "Faith"})

    summary_module._generate_summary(query="q", sources=sources)

    assert fake.calls[0]["extra_body"] == {"reasoning_effort": "none"}


def test_generate_summary_raises_502_on_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    exc = openai.APIError(
        "down",
        httpx.Request("POST", "https://api.ppq.ai/v1/chat/completions"),
        body=None,
    )
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


def test_generate_summary_raises_502_on_empty_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gateway 200 with no choices (e.g. fully safety-blocked) is a 502, not an IndexError."""
    fake = _FakeClient(no_choices=True)
    monkeypatch.setattr(summary_module, "_client", lambda: fake)
    sources = summary_module._build_sources([_hit(1, 0)], {uuid.UUID(int=1): "Faith"})

    with pytest.raises(HTTPException) as excinfo:
        summary_module._generate_summary(query="q", sources=sources)
    assert excinfo.value.status_code == 502


# --- handler ---------------------------------------------------------------


async def test_handler_503_when_key_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing key → 503 *before* retrieval (don't burn ~30s of CPU rerank)."""
    monkeypatch.setattr(summary_module.settings, "llm_provider", "google")
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
    # The detail names the ACTIVE provider's missing env var (Phase 14b).
    assert "GOOGLE_API_KEY" in excinfo.value.detail
    assert called["run_search"] is False


async def test_handler_no_context_message_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summary_module.settings, "llm_provider", "google")
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
    monkeypatch.setattr(summary_module.settings, "llm_provider", "google")
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
    assert resp.citations[0].content == "grace abounds"


# --- provider resolution (Phase 14b, ADR 0005) -------------------------------


def test_provider_defaults_to_google(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh env (no SERMON_API_LLM_* vars) resolves to the google arm."""
    monkeypatch.delenv("SERMON_API_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("SERMON_API_LLM_MODEL", raising=False)
    fresh = ApiSettings()
    assert fresh.llm_provider == "google"
    assert fresh.llm_model is None


def test_provider_map_pins_endpoints_models_keys() -> None:
    """``_PROVIDERS`` is the single source of truth — pin every cell (ADR 0005)."""
    google = summary_module._PROVIDERS["google"]
    assert google.base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert google.default_model == "gemini-2.5-flash"
    assert google.key_env_var == "GOOGLE_API_KEY"
    ppq = summary_module._PROVIDERS["ppq"]
    assert ppq.base_url == "https://api.ppq.ai/v1"
    assert ppq.default_model == "google/gemini-2.5-flash"
    assert ppq.key_env_var == "PPQ_API_KEY"
    # Phase 16b: DeepInfra reuses the embeddings base_url + DEEPINFRA_API_KEY.
    deepinfra = summary_module._PROVIDERS["deepinfra"]
    assert deepinfra.base_url == "https://api.deepinfra.com/v1/openai"
    assert deepinfra.default_model == "google/gemini-2.5-flash"
    assert deepinfra.key_env_var == "DEEPINFRA_API_KEY"


def test_deepinfra_flip_constructs_client_with_deepinfra_base_url_and_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider=deepinfra → client built against DeepInfra with DEEPINFRA_API_KEY.

    No silent cross-pairing: a configured GOOGLE/PPQ key must not satisfy the
    deepinfra arm.
    """
    monkeypatch.setattr(summary_module.settings, "llm_provider", "deepinfra")
    monkeypatch.setattr(summary_module.settings, "deepinfra_api_key", "di-test")
    monkeypatch.setattr(summary_module.settings, "google_api_key", None)
    monkeypatch.setattr(summary_module.settings, "ppq_api_key", None)
    summary_module._client.cache_clear()
    try:
        client = summary_module._client()
        assert str(client.base_url).rstrip("/") == "https://api.deepinfra.com/v1/openai"
        assert client.api_key == "di-test"
    finally:
        summary_module._client.cache_clear()


def test_ppq_flip_constructs_client_with_ppq_base_url_and_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider=ppq → the real client is built against ppq.ai with PPQ_API_KEY."""
    monkeypatch.setattr(summary_module.settings, "llm_provider", "ppq")
    monkeypatch.setattr(summary_module.settings, "ppq_api_key", "pk-test")
    monkeypatch.setattr(summary_module.settings, "google_api_key", None)
    summary_module._client.cache_clear()
    try:
        client = summary_module._client()
        assert str(client.base_url).rstrip("/") == "https://api.ppq.ai/v1"
        assert client.api_key == "pk-test"
    finally:
        # Never leak a cached test client into another test's process state.
        summary_module._client.cache_clear()


def test_ppq_flip_uses_ppq_default_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """provider=ppq → the "google/"-prefixed catalog spelling, not the bare id."""
    monkeypatch.setattr(summary_module.settings, "llm_provider", "ppq")
    monkeypatch.setattr(summary_module.settings, "llm_model", None)
    fake = _FakeClient()
    monkeypatch.setattr(summary_module, "_client", lambda: fake)
    sources = summary_module._build_sources([_hit(1, 0)], {uuid.UUID(int=1): "Faith"})

    summary_module._generate_summary(query="q", sources=sources)

    assert fake.calls[0]["model"] == "google/gemini-2.5-flash"


def test_reasoning_effort_empty_env_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose's ``${VAR:-}`` delivers "" — must validate to None, not explode.

    The prod compose passes SERMON_API_LLM_REASONING_EFFORT through with an
    empty default; without the before-validator an empty string would fail
    the Literal validation at boot.
    """
    from settings import ApiSettings

    monkeypatch.setenv("SERMON_API_LLM_REASONING_EFFORT", "")
    assert ApiSettings().llm_reasoning_effort is None
    monkeypatch.setenv("SERMON_API_LLM_REASONING_EFFORT", "none")
    assert ApiSettings().llm_reasoning_effort == "none"


def test_llm_model_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """SERMON_API_LLM_MODEL beats the active provider's default."""
    monkeypatch.setattr(summary_module.settings, "llm_provider", "ppq")
    monkeypatch.setattr(summary_module.settings, "llm_model", "google/gemini-2.5-flash-lite")
    fake = _FakeClient()
    monkeypatch.setattr(summary_module, "_client", lambda: fake)
    sources = summary_module._build_sources([_hit(1, 0)], {uuid.UUID(int=1): "Faith"})

    summary_module._generate_summary(query="q", sources=sources)

    assert fake.calls[0]["model"] == "google/gemini-2.5-flash-lite"


async def test_handler_503_detail_names_ppq_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """provider=ppq, key unset → 503 names PPQ_API_KEY; a configured
    GOOGLE_API_KEY must not satisfy the ppq arm (no silent cross-pairing)."""
    monkeypatch.setattr(summary_module.settings, "llm_provider", "ppq")
    monkeypatch.setattr(summary_module.settings, "ppq_api_key", None)
    monkeypatch.setattr(summary_module.settings, "google_api_key", "gk")
    user: Any = _FakeUser()
    session: Any = object()

    with pytest.raises(HTTPException) as excinfo:
        await summary_module.search_summary(
            payload=summary_module.SummaryRequest(query="q"),
            current_user=user,
            session=session,
        )
    assert excinfo.value.status_code == 503
    assert "PPQ_API_KEY" in excinfo.value.detail


async def test_handler_503_ppq_key_does_not_satisfy_google_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror image: provider=google ignores a configured PPQ_API_KEY."""
    monkeypatch.setattr(summary_module.settings, "llm_provider", "google")
    monkeypatch.setattr(summary_module.settings, "google_api_key", None)
    monkeypatch.setattr(summary_module.settings, "ppq_api_key", "pk")
    user: Any = _FakeUser()
    session: Any = object()

    with pytest.raises(HTTPException) as excinfo:
        await summary_module.search_summary(
            payload=summary_module.SummaryRequest(query="q"),
            current_user=user,
            session=session,
        )
    assert excinfo.value.status_code == 503
    assert "GOOGLE_API_KEY" in excinfo.value.detail
