"""Pytest fixtures: load HTML/JSON fixtures deterministically, no live HTTP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _read(fixture_name: str) -> str:
    path = FIXTURES_DIR / fixture_name
    return path.read_text(encoding="utf-8")


def _read_json(fixture_name: str) -> dict[str, Any]:
    return json.loads(_read(fixture_name))


@pytest.fixture
def fixtures_dir() -> Path:
    """Path to the tests/fixtures directory."""
    return FIXTURES_DIR


# ---------------------------------------------------------------------------
# /members/search — raw HTML (no JSON wrapper, Content-Type text/html)
# ---------------------------------------------------------------------------


@pytest.fixture
def search_response_html() -> str:
    """Raw HTML response from `/members/search` (NOT JSON-wrapped)."""
    return _read("search_response.html")


# ---------------------------------------------------------------------------
# /members/singlepost — JSON {error, content, header, views}
# ---------------------------------------------------------------------------


@pytest.fixture
def singlepost_response_json() -> dict[str, Any]:
    """Full JSON payload from `/members/singlepost` (content+header+views)."""
    return _read_json("singlepost_response.json")


@pytest.fixture
def singlepost_html() -> str:
    """HTML content from the `content` field of `/members/singlepost`."""
    return _read_json("singlepost_response.json")["content"]


@pytest.fixture
def singlepost_header_html() -> str:
    """HTML content from the `header` field of `/members/singlepost`."""
    return _read_json("singlepost_response.json")["header"]


@pytest.fixture
def singlepost_views_html() -> str:
    """HTML content from the `views` field of `/members/singlepost`."""
    return _read_json("singlepost_response.json")["views"]


# ---------------------------------------------------------------------------
# /members/structure — JSON {error, navigation, content, properties}
# The category tree lives in `navigation` (NOT `content`).
# ---------------------------------------------------------------------------


@pytest.fixture
def structure_html() -> str:
    """HTML content of the `navigation` field from `/members/structure`.

    This is what `parse_structure` consumes — the L1 category tree, NOT the
    landing-page widgets that live in the `content` field.
    """
    return _read_json("structure_response.json")["navigation"]


@pytest.fixture
def structure_response_json() -> dict[str, Any]:
    """Full JSON payload from `/members/structure`."""
    return _read_json("structure_response.json")


# ---------------------------------------------------------------------------
# /members/expandStructure — JSON {error, options, header, structure, content}
# - Intermediate nodes: `structure` has subcategory HTML, `content` is false.
# - Leaf nodes: `content` has a search form (NOT posts), `structure` is false.
# ---------------------------------------------------------------------------


@pytest.fixture
def expand_structure_subcategories_html() -> str:
    """HTML content of the `structure` field for an intermediate expand."""
    return _read_json("expand_structure_intermediate_response.json")["structure"]


@pytest.fixture
def expand_structure_intermediate_response_json() -> dict[str, Any]:
    """Full JSON payload from `/members/expandStructure` (intermediate node)."""
    return _read_json("expand_structure_intermediate_response.json")


@pytest.fixture
def expand_structure_leaf_response_json() -> dict[str, Any]:
    """Full JSON payload from `/members/expandStructure` (leaf node).

    Note: leaf responses return a search form in `content`, NOT post listings.
    Posts in a leaf must be fetched via `/members/search` with
    `conditions[category_level*_id]` params.
    """
    return _read_json("expand_structure_leaf_response.json")


# Legacy raw-HTML expand fixtures (still consumed by parser tests directly).
@pytest.fixture
def expand_structure_leaf_html() -> str:
    """Raw HTML with `.post-widget` containers (legacy/expansion format)."""
    return _read("expand_structure_leaf.html")


# ---------------------------------------------------------------------------
# /post/recentposts — raw HTML (no JSON wrapper, public endpoint)
# ---------------------------------------------------------------------------


@pytest.fixture
def recentposts_html() -> str:
    """Raw HTML response from `/post/recentposts` (NOT JSON-wrapped).

    The verified-live format contains only post widget scaffolds (post_id +
    spinner), NOT titles. Titles are lazy-loaded via `/members/postWidgetFill`.
    """
    return _read("recentposts.html")


# ---------------------------------------------------------------------------
# /members/postWidgetFill — raw HTML (no JSON wrapper)
# ---------------------------------------------------------------------------


@pytest.fixture
def postwidgetfill_3103_html() -> str:
    """Raw HTML from `/members/postWidgetFill` for post_id=3103."""
    return _read("postwidgetfill_3103.html")


@pytest.fixture
def postwidgetfill_3201_html() -> str:
    """Raw HTML from `/members/postWidgetFill` for post_id=3201."""
    return _read("postwidgetfill_3201.html")


@pytest.fixture
def postwidgetfill_3202_html() -> str:
    """Raw HTML from `/members/postWidgetFill` for post_id=3202."""
    return _read("postwidgetfill_3202.html")


@pytest.fixture
def postwidgetfill_3203_html() -> str:
    """Raw HTML from `/members/postWidgetFill` for post_id=3203."""
    return _read("postwidgetfill_3203.html")
