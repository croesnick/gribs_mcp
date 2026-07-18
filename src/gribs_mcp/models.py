"""Pydantic v2 models for all gribs_mcp tool outputs.

Every model returning politically relevant data includes `url` and `retrieved_at`
(Quellenpflicht, INTENT.md §Tool-Design & §Guardrails). All datetimes are UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    """Return current UTC time (tz-aware)."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# TypedDict contracts for gribs.net API responses were removed (Issue #6).
# The `/members/*` JSON endpoints return loosely-typed `{error, ...}` payloads
# that parsers validate defensively at runtime (isinstance checks). Parsers
# now accept `Mapping[str, Any]` instead of per-endpoint TypedDicts, which
# were pure documentation and served only to justify `cast()` calls in the
# client.


class GribsError(BaseModel):
    """Typed error envelope for non-fatal, surfaced failures."""

    model_config = ConfigDict(frozen=True)

    code: Annotated[str, Field(description="Machine-readable error code.")]
    message: Annotated[str, Field(description="Human-readable error message.")]


class SearchHit(BaseModel):
    """Single result from `/members/search`."""

    model_config = ConfigDict(frozen=True)

    title: Annotated[str, Field(description="Title of the matching post.")]
    snippet: Annotated[
        str,
        Field(
            description="Short excerpt around the match (highlight markers stripped)."
        ),
    ]
    wp_id: Annotated[
        int | None,
        Field(
            description="WordPress post id from the `?wp=<id>` deep link, if present."
        ),
    ]
    url: Annotated[str, Field(description="Canonical deep-link URL on gribs.net.")]
    retrieved_at: Annotated[
        datetime, Field(description="UTC timestamp when the hit was retrieved.")
    ]


class PostTeaser(BaseModel):
    """Teaser for a post in a category listing or recent list."""

    model_config = ConfigDict(frozen=True)

    post_id: Annotated[
        int, Field(description="Internal gribs post id used by `/members/singlepost`.")
    ]
    title: Annotated[str, Field(description="Post title.")]
    date: Annotated[
        str | None,
        Field(description="Display date as shown on gribs.net, ISO-ish if parseable."),
    ]
    snippet: Annotated[
        str | None, Field(description="Short preview text, if available.")
    ]
    url: Annotated[
        str,
        Field(
            description=(
                "Canonical deep-link URL. Uses `?wp=<id>` or `?h=<hash>` when "
                "known; falls back to the members landing page when only "
                "post_id is available (gribs.net has no `?p=<id>` deep-link)."
            )
        ),
    ]
    retrieved_at: Annotated[
        datetime, Field(description="UTC timestamp when the teaser was retrieved.")
    ]


class PostDetail(BaseModel):
    """Full detail view of a single post from `/members/singlepost`."""

    model_config = ConfigDict(frozen=True)

    post_id: Annotated[
        int | None,
        Field(
            description=(
                "Internal gribs post id, inferred from the response "
                "(data-heart attribute, hashfield id, or postWidget call in "
                "the `content` field). None if inference failed; callers "
                "should treat None as a trust-failure and refuse to act on "
                "the post."
            )
        ),
    ]
    title: Annotated[str, Field(description="Post title.")]
    date: Annotated[
        str | None, Field(description="Display date as shown on gribs.net.")
    ]
    view_count: Annotated[
        int | None, Field(description="Number of views, if reported.")
    ]
    share_url: Annotated[
        str | None, Field(description="`?h=<hash>` share URL, if present.")
    ]
    category_breadcrumb: Annotated[
        list[str],
        Field(description="Breadcrumb path from top-level category down to the post."),
    ]
    body_html: Annotated[str, Field(description="Raw HTML body of the post.")]
    body_text: Annotated[
        str, Field(description="Cleaned plaintext body (trafilatura-extracted).")
    ]
    url: Annotated[str, Field(description="Canonical deep-link URL on gribs.net.")]
    retrieved_at: Annotated[
        datetime, Field(description="UTC timestamp when the post was retrieved.")
    ]


class CategoryNode(BaseModel):
    """A node in the gribs category tree (recursive)."""

    model_config = ConfigDict(frozen=True)

    id: Annotated[
        int, Field(description="Category id as used in `obj[cat]` / `obj[l1]` / etc.")
    ]
    label: Annotated[str, Field(description="Human-readable category label.")]
    children: Annotated[
        list[CategoryNode],
        Field(description="Sub-categories; empty for leaves."),
    ]


class StructureExpansion(BaseModel):
    """Result of expanding a structure node.

    Either `subcategories` (intermediate node) or `posts` (leaf node) is populated.
    Both are optional because the API returns one or the other depending on depth.
    """

    model_config = ConfigDict(frozen=True)

    subcategories: Annotated[
        list[CategoryNode] | None,
        Field(default=None, description="Sub-categories if this node has children."),
    ]
    posts: Annotated[
        list[PostTeaser] | None,
        Field(default=None, description="Post teasers if this node is a leaf."),
    ]


class PostIdRef(BaseModel):
    """Result of resolving a WordPress `wp_id` to an internal `post_id`.

    Returned by `resolve_post_id`. Carries `url` + `retrieved_at`
    (Quellenpflicht) so callers can cite where the resolution came from.
    """

    model_config = ConfigDict(frozen=True)

    wp_id: Annotated[
        int, Field(description="WordPress post id (from `?wp=<id>` deep links).")
    ]
    post_id: Annotated[
        int,
        Field(
            description=(
                "Internal gribs post id (usable with `/members/singlepost` "
                "and `get_antrag`)."
            )
        ),
    ]
    url: Annotated[
        str,
        Field(description="The `?wp=<id>` deep-link URL that was resolved."),
    ]
    retrieved_at: Annotated[
        datetime, Field(description="UTC timestamp when the resolution was performed.")
    ]


class Download(BaseModel):
    """A downloadable file (typically a PDF) extracted from a post body.

    Returned by `extract_downloads`. Carries `source_url` + `retrieved_at`
    (Quellenpflicht) so the download can be cited back to its origin post.
    """

    model_config = ConfigDict(frozen=True)

    url: Annotated[
        str,
        Field(
            description=(
                "Absolute download URL (relative URLs resolved against gribs.net)."
            )
        ),
    ]
    link_text: Annotated[str, Field(description="Anchor text of the download link.")]
    filename: Annotated[
        str | None,
        Field(description="Filename extracted from the URL path, if present."),
    ]
    is_pdf: Annotated[
        bool,
        Field(description="True if the URL path ends in `.pdf` (case-insensitive)."),
    ]
    source_post_id: Annotated[
        int, Field(description="post_id of the post this download was extracted from.")
    ]
    source_url: Annotated[
        str,
        Field(description="Canonical URL of the source post (Quellenpflicht)."),
    ]
    retrieved_at: Annotated[
        datetime, Field(description="UTC timestamp when the download was extracted.")
    ]


__all__ = [
    "CategoryNode",
    "Download",
    "GribsError",
    "PostDetail",
    "PostIdRef",
    "PostTeaser",
    "SearchHit",
    "StructureExpansion",
]
