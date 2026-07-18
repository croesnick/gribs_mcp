"""FastMCP server instance and tool definitions for gribs_mcp.

All tools are read-only (`readOnlyHint=True`), async, and clamp `limit`
parameters per INTENT.md §Tool-Design.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from gribs_mcp.client import GribsApiError, get_client
from gribs_mcp.models import (
    CategoryNode,
    PostDetail,
    PostTeaser,
    SearchHit,
    StructureExpansion,
)

# Category name -> category_id mapping (INTENT.md §"Sektionen (Members-Bereich)").
# Antragsbörse=1 is verified. Other ids are TODO (INTENT.md says "zu verifizieren")
# and therefore mapped to None to fail loudly rather than silently hit wrong data.
CATEGORY_IDS: dict[str, int | None] = {
    "Antragsbörse": 1,
    "Wissenswert": None,  # TODO: verify cat_id (likely 2)
    "Arbeit im Rat": None,  # TODO: verify cat_id (likely 3)
    "Mitgliederbriefe": None,  # TODO: verify cat_id (likely 4)
    "DenkWerkstatt": None,  # TODO: verify cat_id (likely 5)
    "Mitgliederversammlungen": None,  # TODO: verify cat_id (likely 6)
    "Kommunalwahl": None,  # TODO: verify cat_id (likely 7)
}

DEFAULT_CATEGORY = "Antragsbörse"
MAX_SEARCH_LIMIT = 50  # API cap per INTENT.md §API-Endpoints
MAX_RECENT_LIMIT = 3  # /post/recentposts returns exactly 3
MAX_CATEGORY_LIMIT = 100  # expandStructure listings

INSTRUCTIONS = (
    "MCP server for gribs.net (Grünen-interne Plattform). "
    "Tools: search_antraege, get_antrag, list_categories, "
    "list_antraege_in_category, recent_posts. "
    "Quellenpflicht: jeder Antrag wird mit URL und Abrufdatum zurückgegeben. "
    "Authentication is handled automatically via cached session cookies."
)

mcp = FastMCP("gribs-mcp", instructions=INSTRUCTIONS)

# Shared read-only tool annotation (no tool writes back to gribs.net).
READ_ONLY_ANNOTATIONS = ToolAnnotations(readOnlyHint=True)


def _format_known_categories() -> list[str]:
    """Format CATEGORY_IDS for error messages, marking verification status."""
    result: list[str] = []
    for name, cat_id in CATEGORY_IDS.items():
        status = "verified" if cat_id is not None else "unverified"
        result.append(f"{name} ({status})")
    return sorted(result)


def _resolve_category_id(category: str) -> int:
    """Resolve a category name to its numeric id, raising on unknown/unverified."""
    cat_id = CATEGORY_IDS.get(category)
    if cat_id is None:
        if category in CATEGORY_IDS:
            raise GribsApiError(
                f"Category '{category}' is registered but its cat_id "
                "is not yet verified — refusing to query with an unverified id."
            )
        raise GribsApiError(
            f"Unknown category '{category}'. Known: {_format_known_categories()}"
        )
    return cat_id


def _clamp(limit: int, max_value: int) -> int:
    """Clamp a limit to [1, max_value] per INTENT.md §Tool-Design."""
    return max(1, min(limit, max_value))


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
async def search_antraege(
    query: Annotated[str, Field(description="Full-text search query.")],
    category: Annotated[
        str,
        Field(
            default=DEFAULT_CATEGORY,
            description=(
                "Category name (e.g. 'Antragsbörse'). "
                "See CATEGORY_IDS for known values."
            ),
        ),
    ] = DEFAULT_CATEGORY,
    whole_word: Annotated[
        bool,
        Field(default=False, description="If true, request whole-word matches only."),
    ] = False,
    limit: Annotated[
        int,
        Field(
            default=25,
            description="Maximum number of hits to return (clamped to API max).",
        ),
    ] = 25,
) -> list[SearchHit]:
    """Search gribs.net posts (default: Antragsbörse).

    Args:
        query: Full-text search query.
        category: Category name (default: Antragsbörse).
        whole_word: If true, request whole-word matches.
        limit: Maximum hits to return; clamped to [1, 50] (API cap).

    Returns:
        Up to `limit` SearchHit objects, each with title, snippet, wp_id, url,
        and retrieved_at (Quellenpflicht).

    Raises:
        GribsApiError: if the category is unknown or its cat_id is unverified.
        GribsAuthError: if authentication fails.
    """
    category_id = _resolve_category_id(category)
    limit = _clamp(limit, MAX_SEARCH_LIMIT)
    client = get_client()
    hits = await client.search(
        query=query,
        category_id=category_id,
        whole_word=whole_word,
    )
    return hits[:limit]


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
async def get_antrag(
    post_id: Annotated[int, Field(description="Internal gribs post id (NOT wp_id).")],
) -> PostDetail:
    """Fetch a single post (antrag) in full detail.

    The client verifies that the parsed `post_id` matches the requested one;
    a mismatch raises `GribsApiError` (silent-wrong-post guard).

    Args:
        post_id: Internal gribs post id (as returned by list_antraege_in_category
            or inferred from a wp_id via postWidgetFill — TODO).

    Returns:
        PostDetail with full body, metadata, breadcrumb, url, retrieved_at.

    Raises:
        GribsAuthError: if authentication fails.
        GribsApiError: if the post cannot be fetched, or if the returned
            post_id doesn't match the requested one.
    """
    client = get_client()
    return await client.get_post(post_id)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
async def list_categories(
    category: Annotated[
        str,
        Field(
            default=DEFAULT_CATEGORY,
            description="Category name to enumerate (default: Antragsbörse).",
        ),
    ] = DEFAULT_CATEGORY,
) -> CategoryNode:
    """List the top-level (L1) categories within a gribs section.

    Args:
        category: Category name (default: Antragsbörse).

    Returns:
        CategoryNode with L1 children.

    Raises:
        GribsApiError: if the category is unknown or its cat_id is unverified.
        GribsAuthError: if authentication fails.
    """
    category_id = _resolve_category_id(category)
    client = get_client()
    return await client.list_categories(category_id=category_id)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
async def list_antraege_in_category(
    category: Annotated[
        str,
        Field(
            default=DEFAULT_CATEGORY,
            description="Category name (default: Antragsbörse).",
        ),
    ] = DEFAULT_CATEGORY,
    l1: Annotated[
        int | None, Field(default=None, description="L1 sub-category id.")
    ] = None,
    l2: Annotated[
        int | None, Field(default=None, description="L2 sub-category id.")
    ] = None,
    l3: Annotated[
        int | None, Field(default=None, description="L3 sub-category id.")
    ] = None,
    limit: Annotated[
        int,
        Field(default=50, description="Maximum teasers to return (clamped)."),
    ] = 50,
) -> StructureExpansion:
    """Browse the category tree without a search query.

    Drill into the category tree by providing l1/l2/l3 ids (returned by
    `list_categories` or a previous `list_antraege_in_category` call).
    Intermediate nodes return subcategories; leaf nodes return an empty
    expansion (both `subcategories` and `posts` are None) — see the note below.

    **Leaf limitation (L2)**: gribs.net's `/members/expandStructure` endpoint
    returns a *search form* (not a post listing) when called on a leaf
    subcategory. There is no direct "list all posts in this subcategory"
    endpoint. To retrieve posts in a specific subcategory, use
    `search_antraege` with a broad query — the search endpoint supports
    scoped search via `conditions[category_level1_id]` / `[l2]` / `[l3]`
    (the client applies these automatically when you pass `l1`/`l2`/`l3`
    to `search_antraege`... but note: the MCP tool `search_antraege` does
    NOT currently expose `l1`/`l2`/`l3` params. For scoped search today,
    use the client directly or extend `search_antraege` in a follow-up).

    Args:
        category: Category name (default: Antragsbörse).
        l1: L1 sub-category id (or None for L1 root).
        l2: L2 sub-category id.
        l3: L3 sub-category id.
        limit: Maximum teasers to return (clamped to [1, 100]). Only applies
            when the expansion returns posts (currently never for live
            leaf responses — kept for future use).

    Returns:
        StructureExpansion: `subcategories` populated for intermediate nodes;
        both fields None for leaf nodes (use `search_antraege` instead).

    Raises:
        GribsApiError: if the category is unknown/unverified.
        GribsAuthError: if authentication fails.
    """
    category_id = _resolve_category_id(category)
    limit = _clamp(limit, MAX_CATEGORY_LIMIT)
    client = get_client()
    expansion = await client.expand_structure(
        category_id=category_id,
        l1=l1,
        l2=l2,
        l3=l3,
    )
    if expansion.posts is not None:
        expansion = expansion.model_copy(
            update={"posts": expansion.posts[:limit]},
        )
    return expansion


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
async def recent_posts(
    limit: Annotated[
        int,
        Field(default=3, description="Maximum teasers to return (clamped)."),
    ] = 3,
) -> list[PostTeaser]:
    """Fetch the newest posts from the public gribs.net homepage.

    Args:
        limit: Maximum teasers to return (clamped to [1, 3]).

    Returns:
        Up to `limit` PostTeaser objects with url + retrieved_at (Quellenpflicht).

    Raises:
        GribsApiError: on API failure.
    """
    limit = _clamp(limit, MAX_RECENT_LIMIT)
    client = get_client()
    posts = await client.recent_posts()
    return posts[:limit]


__all__ = [
    "CATEGORY_IDS",
    "DEFAULT_CATEGORY",
    "INSTRUCTIONS",
    "MAX_CATEGORY_LIMIT",
    "MAX_RECENT_LIMIT",
    "MAX_SEARCH_LIMIT",
    "READ_ONLY_ANNOTATIONS",
    "mcp",
    "recent_posts",
    "get_antrag",
    "list_antraege_in_category",
    "list_categories",
    "search_antraege",
]
