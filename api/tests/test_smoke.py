"""Cheap smoke test verifying the api package's pyproject.toml parses."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_pyproject_parses() -> None:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    assert pyproject.exists()
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    assert data["project"]["name"] == "sermon-api"


def test_healthz_route_is_registered() -> None:
    """Cheap import-time check that main.app wires the meta route."""
    from fastapi.routing import APIRoute

    from main import app

    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/healthz" in paths
    assert "/readyz" in paths
    assert "/metrics" in paths
    assert "/auth/signup" in paths
    assert "/auth/login" in paths
    assert "/upload" in paths
    assert "/tasks/{task_id}" in paths
    assert "/library" in paths
    assert "/books/{book_id}/chunks" in paths
    assert "/books/{book_id}/position" in paths
    assert "/documents" in paths
    assert "/documents/{document_id}" in paths
    assert "/documents/{document_id}/restore" in paths
    assert "/documents/{document_id}/export.docx" in paths
    assert "/documents/{document_id}/import" in paths
    assert "/calendar/events" in paths
    assert "/calendar/events/{event_id}" in paths
    assert "/collections" in paths
    assert "/collections/{collection_id}" in paths
    assert "/collections/{collection_id}/books" in paths
    assert "/search" in paths
    assert "/search-summary" in paths
    assert "/search-history" in paths
    assert "/search-history/{history_id}" in paths
