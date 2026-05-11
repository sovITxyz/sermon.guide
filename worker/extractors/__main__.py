"""CLI entry point: `python -m extractors <path>` prints Markdown to stdout."""

from __future__ import annotations

from .extract import main

if __name__ == "__main__":
    raise SystemExit(main())
