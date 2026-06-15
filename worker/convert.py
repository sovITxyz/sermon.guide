"""DOCX round-trip for sermon manuscripts (Phase 43).

The canonical document format is TipTap/ProseMirror JSON (stored in
``documents.content`` JSONB). This module converts between that JSON and a
Word ``.docx`` so a sermon can be exported for editing in Word and re-imported.

Pipeline (both legs go through HTML — the format both TipTap and pandoc speak):

* **export** ``content_json -> HTML -> .docx``

  1. The React-free Node leg (``convert_node/cli.mjs export``) turns the
     ProseMirror JSON into HTML via ``@tiptap/html``'s ``generateHTML`` using the
     SAME extension set as the editor (StarterKit + the citation node).
  2. ``pypandoc`` converts that HTML to ``.docx`` with a ``--reference-doc``
     template (pandoc's default reference doc, shipped in ``assets/``).

* **import** ``.docx -> HTML -> content_json``

  1. ``pypandoc`` converts the ``.docx`` to HTML.
  2. The Node leg (``convert_node/cli.mjs import``) turns that HTML into
     ProseMirror JSON via ``generateJSON`` with the same extension set.

CITATION FIDELITY: ``data-*`` attributes do NOT survive a ``.docx`` round-trip,
only hyperlinks do. The citation node therefore serializes to an
``<a href="/read/{bookId}?chunk={chunkIndex}">`` (see
``convert_node/citation-extension.mjs``) so the deep-link survives; on import the
``bookId`` + ``chunkIndex`` are recovered from that URL. ``bookTitle`` is
degraded-from-anchor-text and ``snippet`` / ``parentSection`` are lost — the
accepted fidelity ceiling.

DEPENDENCY BOUNDARY (enforced by api/AGENTS.md + worker/AGENTS.md): this module
imports ``pypandoc`` DIRECTLY and shells out to Node. It MUST NOT import
``worker.extractors`` / ``ingest`` / ``chunking`` / ``dedup`` / ``celery_app`` /
``tasks.*`` — it is the one worker module ``api/`` is allowed to import, and it
stays free of the ingestion graph.

SYSTEM BINARIES: needs ``pandoc`` and Node 22 on the host, plus the
``convert_node`` bundle's ``node_modules`` populated (``npm install`` in
``worker/convert_node/``). See worker/AGENTS.md "System binaries". These become
api+worker image deps for Phase 29 to bake.
"""

# pypandoc ships no PEP 561 type stubs (same relaxation as extractors/epub.py).
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import json
import shutil
import subprocess  # noqa: S404 — fixed argv to the bundled Node CLI; never shell=True, never user-controlled.
import sys
import tempfile
from pathlib import Path
from typing import Any

import pypandoc

# The worker package root, used to locate the Node CLI and the reference docx.
_WORKER_ROOT = Path(__file__).resolve().parent
_CONVERT_NODE_DIR = _WORKER_ROOT / "convert_node"
_NODE_CLI = _CONVERT_NODE_DIR / "cli.mjs"
_REFERENCE_DOCX = _WORKER_ROOT / "assets" / "reference.docx"

# Hard wall-clock ceiling on the Node leg so a pathological document can never
# hang a request thread. Generous — generateHTML/generateJSON on a sermon-sized
# doc is milliseconds.
_NODE_TIMEOUT_S = 60


class ConversionError(RuntimeError):
    """A conversion leg (Node or pandoc) failed.

    Raised instead of leaking a raw ``CalledProcessError``/``OSError`` so callers
    (the API import/export routes) can map it to a clean 5xx without a stack
    trace oracle.
    """


def _run_node(mode: str, *, stdin_data: str) -> str:
    """Run the Node CLI in *mode* (``export``/``import``), piping *stdin_data*.

    Returns the CLI's stdout. The argv is fixed (the bundled ``cli.mjs`` path +
    a literal mode); nothing here is shell-interpolated or attacker-controlled,
    so this is not a shell-injection surface.
    """
    if mode not in ("export", "import"):  # defensive: callers pass literals
        msg = f"unknown convert_node mode: {mode!r}"
        raise ConversionError(msg)
    if not _NODE_CLI.exists():
        msg = (
            f"convert_node CLI not found at {_NODE_CLI}; run `npm install` in "
            f"{_CONVERT_NODE_DIR} to populate node_modules"
        )
        raise ConversionError(msg)
    # Resolve `node` to an absolute path up front (no partial-path exec; the
    # binary is found on PATH exactly as pypandoc finds pandoc).
    node_bin = shutil.which("node")
    if node_bin is None:
        msg = "node binary not found on PATH (Node 22 required for @tiptap/html)"
        raise ConversionError(msg)
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, trusted CLI path.
            [node_bin, str(_NODE_CLI), mode],
            input=stdin_data.encode("utf-8"),
            capture_output=True,
            cwd=str(_CONVERT_NODE_DIR),
            timeout=_NODE_TIMEOUT_S,
            check=True,
        )
    except FileNotFoundError as exc:  # `node` vanished between which() and exec
        msg = "node binary not found on PATH (Node 22 required for @tiptap/html)"
        raise ConversionError(msg) from exc
    except subprocess.TimeoutExpired as exc:
        msg = f"convert_node {mode} timed out after {_NODE_TIMEOUT_S}s"
        raise ConversionError(msg) from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        msg = f"convert_node {mode} failed (exit {exc.returncode}): {stderr.strip()}"
        raise ConversionError(msg) from exc
    return proc.stdout.decode("utf-8")


def convert_to_docx(content_json: dict[str, Any]) -> bytes:
    """Export a ProseMirror ``content`` document to ``.docx`` bytes.

    *content_json* is the TipTap document (``{"type": "doc", "content": [...]}``).
    Returns the raw ``.docx`` file bytes, ready to stream as a download.
    """
    html = _run_node("export", stdin_data=json.dumps(content_json))
    if not _REFERENCE_DOCX.exists():
        msg = f"reference docx template missing at {_REFERENCE_DOCX}"
        raise ConversionError(msg)
    # pandoc writes binary docx to a file (not stdout); stage it in /tmp and read
    # it back. The reference-doc carries the document styles.
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
        out_path = Path(tmp.name)
    try:
        try:
            pypandoc.convert_text(
                html,
                to="docx",
                format="html",
                outputfile=str(out_path),
                extra_args=["--reference-doc", str(_REFERENCE_DOCX)],
            )
        except (RuntimeError, OSError) as exc:
            msg = f"pandoc html->docx failed: {exc}"
            raise ConversionError(msg) from exc
        return out_path.read_bytes()
    finally:
        out_path.unlink(missing_ok=True)


def convert_from_docx(docx_bytes: bytes) -> dict[str, Any]:
    """Import ``.docx`` bytes to a ProseMirror ``content`` document.

    *docx_bytes* is an uploaded Word file (already MIME-sniffed + size-capped by
    the caller). Returns the TipTap document JSON. The caller snapshots the prior
    content and re-derives ``content_text`` itself — this only does the format
    conversion, never trusts or persists anything.
    """
    # pandoc reads docx from a file (it inspects the zip container), so stage the
    # upload in /tmp and always clean it up.
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
        tmp.write(docx_bytes)
        in_path = Path(tmp.name)
    try:
        try:
            html = pypandoc.convert_file(str(in_path), to="html", format="docx")
        except (RuntimeError, OSError) as exc:
            msg = f"pandoc docx->html failed: {exc}"
            raise ConversionError(msg) from exc
    finally:
        in_path.unlink(missing_ok=True)

    raw = _run_node("import", stdin_data=html)
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = "convert_node import returned invalid JSON"
        raise ConversionError(msg) from exc
    if not isinstance(parsed, dict):
        msg = "convert_node import did not return a ProseMirror document object"
        raise ConversionError(msg)
    return parsed


if __name__ == "__main__":  # pragma: no cover - manual smoke helper
    # `python convert.py export < doc.json > out.docx`
    # `python convert.py import < in.docx > doc.json`  (binary docx on stdin)
    _mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if _mode == "export":
        _doc = json.load(sys.stdin)
        sys.stdout.buffer.write(convert_to_docx(_doc))
    elif _mode == "import":
        _out = convert_from_docx(sys.stdin.buffer.read())
        sys.stdout.write(json.dumps(_out))
    else:
        sys.stderr.write("usage: python convert.py <export|import>\n")
        raise SystemExit(2)
