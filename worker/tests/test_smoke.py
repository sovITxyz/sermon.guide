"""Cheap smoke test verifying the worker package's pyproject.toml parses."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_pyproject_parses() -> None:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    assert pyproject.exists()
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    assert data["project"]["name"] == "sermon-worker"
