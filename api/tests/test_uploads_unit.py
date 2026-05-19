"""Unit tests for upload helpers — filename sanitization."""

# Tests exercise module-internals on purpose.
# pyright: reportPrivateUsage=false

from __future__ import annotations

from uploads import _sanitize_filename


def test_sanitize_strips_path_traversal() -> None:
    # Multipart filenames are client-supplied; a `../../etc/passwd` value
    # without sanitization would drop the file outside settings.upload_dir.
    assert _sanitize_filename("../../etc/passwd") == "passwd"
    assert _sanitize_filename("/abs/path/book.epub") == "book.epub"
    # Backslashes are normalized to forward slashes before Path.name so
    # Windows-style paths get fully stripped, not just collapsed.
    assert _sanitize_filename("..\\..\\windows\\sys.epub") == "sys.epub"


def test_sanitize_collapses_unsafe_chars() -> None:
    assert _sanitize_filename("naïve $book; rm -rf.epub") == "na_ve__book__rm_-rf.epub"


def test_sanitize_handles_empty_and_dotfile() -> None:
    assert _sanitize_filename(None) == "upload.bin"
    assert _sanitize_filename("") == "upload.bin"
    # Leading dots are stripped so an attacker can't smuggle hidden files.
    assert _sanitize_filename(".hidden") == "hidden"
    assert _sanitize_filename("...") == "upload.bin"
