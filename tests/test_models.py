"""Pydantic model validation tests — focus on Quellenpflicht (url + retrieved_at)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from gribs_mcp.models import (
    CategoryNode,
    GribsError,
    PostDetail,
    PostTeaser,
    SearchHit,
    StructureExpansion,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TestSearchHit:
    """SearchHit must enforce Quellenpflicht (url + retrieved_at)."""

    def test_minimal_valid(self) -> None:
        hit = SearchHit(
            title="Test",
            snippet="snippet",
            wp_id=123,
            url="https://www.gribs.net/?wp=123",
            retrieved_at=_utcnow(),
        )
        assert hit.title == "Test"
        assert hit.wp_id == 123
        assert hit.url.endswith("wp=123")

    def test_url_is_required(self) -> None:
        with pytest.raises(ValidationError):
            SearchHit(  # type: ignore[call-arg]
                title="Test",
                snippet="snippet",
                wp_id=1,
                retrieved_at=_utcnow(),
            )

    def test_retrieved_at_is_required(self) -> None:
        with pytest.raises(ValidationError):
            SearchHit(  # type: ignore[call-arg]
                title="Test",
                snippet="snippet",
                wp_id=1,
                url="https://www.gribs.net/?wp=1",
            )

    def test_wp_id_allows_none(self) -> None:
        hit = SearchHit(
            title="Test",
            snippet="snippet",
            wp_id=None,
            url="https://www.gribs.net/?p=1",
            retrieved_at=_utcnow(),
        )
        assert hit.wp_id is None

    def test_frozen(self) -> None:
        hit = SearchHit(
            title="Test",
            snippet="",
            wp_id=1,
            url="https://www.gribs.net/?wp=1",
            retrieved_at=_utcnow(),
        )
        with pytest.raises(ValidationError):
            hit.title = "Other"  # type: ignore[misc]


class TestPostTeaser:
    def test_minimal_valid(self) -> None:
        teaser = PostTeaser(
            post_id=3102,
            title="Title",
            date="2024-03-12",
            snippet="preview",
            url="https://www.gribs.net/?p=3102",
            retrieved_at=_utcnow(),
        )
        assert teaser.post_id == 3102
        assert teaser.url.endswith("p=3102")

    def test_url_required(self) -> None:
        with pytest.raises(ValidationError):
            PostTeaser(  # type: ignore[call-arg]
                post_id=1,
                title="x",
                date=None,
                snippet=None,
                retrieved_at=_utcnow(),
            )

    def test_retrieved_at_required(self) -> None:
        with pytest.raises(ValidationError):
            PostTeaser(  # type: ignore[call-arg]
                post_id=1,
                title="x",
                date=None,
                snippet=None,
                url="https://www.gribs.net/?p=1",
            )

    def test_optional_fields_accept_none(self) -> None:
        teaser = PostTeaser(
            post_id=1,
            title="x",
            date=None,
            snippet=None,
            url="https://www.gribs.net/?p=1",
            retrieved_at=_utcnow(),
        )
        assert teaser.date is None
        assert teaser.snippet is None


class TestPostDetail:
    def test_full_valid(self) -> None:
        post = PostDetail(
            post_id=3102,
            title="Title",
            date="2024-03-12",
            view_count=42,
            share_url="https://www.gribs.net/?h=abc123",
            category_breadcrumb=["Antragsbörse", "Umwelt"],
            body_html="<p>body</p>",
            body_text="body",
            url="https://www.gribs.net/?h=abc123",
            retrieved_at=_utcnow(),
        )
        assert post.view_count == 42
        assert post.category_breadcrumb == ["Antragsbörse", "Umwelt"]

    def test_url_and_retrieved_at_required(self) -> None:
        with pytest.raises(ValidationError):
            PostDetail(  # type: ignore[call-arg]
                post_id=1,
                title="x",
                date=None,
                view_count=None,
                share_url=None,
                category_breadcrumb=[],
                body_html="",
                body_text="",
            )

    def test_breadcrumb_defaults_to_empty_list(self) -> None:
        # category_breadcrumb is `list[str]` (no default) — required.
        with pytest.raises(ValidationError):
            PostDetail(  # type: ignore[call-arg]
                post_id=1,
                title="x",
                date=None,
                view_count=None,
                share_url=None,
                body_html="",
                body_text="",
                url="https://www.gribs.net/?p=1",
                retrieved_at=_utcnow(),
            )

    def test_post_id_allows_none(self) -> None:
        # post_id is int | None — None is valid (used when inference fails).
        post = PostDetail(
            post_id=None,
            title="x",
            date=None,
            view_count=None,
            share_url=None,
            category_breadcrumb=[],
            body_html="",
            body_text="",
            url="https://www.gribs.net/members/home",
            retrieved_at=_utcnow(),
        )
        assert post.post_id is None


class TestCategoryNode:
    def test_recursive(self) -> None:
        node = CategoryNode(
            id=1,
            label="root",
            children=[
                CategoryNode(id=2, label="child1", children=[]),
                CategoryNode(
                    id=3,
                    label="child2",
                    children=[CategoryNode(id=4, label="grandchild", children=[])],
                ),
            ],
        )
        assert node.children[1].children[0].label == "grandchild"

    def test_empty_children(self) -> None:
        node = CategoryNode(id=1, label="leaf", children=[])
        assert node.children == []


class TestStructureExpansion:
    def test_subcategories_path(self) -> None:
        exp = StructureExpansion(
            subcategories=[CategoryNode(id=1, label="x", children=[])],
            posts=None,
        )
        assert exp.subcategories is not None
        assert exp.posts is None

    def test_posts_path(self) -> None:
        exp = StructureExpansion(
            subcategories=None,
            posts=[
                PostTeaser(
                    post_id=1,
                    title="t",
                    date=None,
                    snippet=None,
                    url="https://www.gribs.net/?p=1",
                    retrieved_at=_utcnow(),
                )
            ],
        )
        assert exp.posts is not None
        assert exp.subcategories is None

    def test_both_optional_default_none(self) -> None:
        exp = StructureExpansion()
        assert exp.subcategories is None
        assert exp.posts is None


class TestGribsError:
    def test_basic(self) -> None:
        err = GribsError(code="AUTH_FAILED", message="Login failed")
        assert err.code == "AUTH_FAILED"
        assert err.message == "Login failed"

    def test_frozen(self) -> None:
        err = GribsError(code="X", message="y")
        with pytest.raises(ValidationError):
            err.code = "Z"  # type: ignore[misc]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
