"""Parser tests against HTML/JSON fixtures (deterministic, no live HTTP)."""

from __future__ import annotations

import pytest

from gribs_mcp.parsers import (
    parse_expand_structure,
    parse_post_widget,
    parse_recent_posts,
    parse_search_results,
    parse_singlepost,
    parse_structure,
)


class TestParseSearchResults:
    """Tests for `parse_search_results`."""

    def test_returns_list(self, search_response_html: str) -> None:
        hits = parse_search_results(search_response_html)
        assert isinstance(hits, list)
        assert len(hits) == 3

    def test_extracts_wp_id_from_onclick(self, search_response_html: str) -> None:
        # The verified live format puts wp_id in the .headline onclick attr,
        # NOT in an <a href>. The parser must extract it from there.
        hits = parse_search_results(search_response_html)
        wp_ids = [hit.wp_id for hit in hits]
        assert wp_ids == [19880, 19881, 19882]

    def test_extracts_title_from_headline(self, search_response_html: str) -> None:
        hits = parse_search_results(search_response_html)
        assert hits[0].title == 'Musterantrag "Unser Wasser schützen"'
        assert hits[1].title == "Positionspapier zur kommunalen Wasserpolitik"

    def test_extracts_snippet_from_bodyline(self, search_response_html: str) -> None:
        hits = parse_search_results(search_response_html)
        # Snippet comes from the .bodyline div, not from a generic container.
        assert "Rekordhitze" in hits[0].snippet
        assert "Wasser" in hits[0].snippet
        # Subsequent hits have their own bodyline snippets.
        assert "Wasser" in hits[1].snippet

    def test_strips_highlight_markers_keeps_text_in_title(
        self, search_response_html: str
    ) -> None:
        hits = parse_search_results(search_response_html)
        # The title in the fixture wraps "Wasser" in <span class="highlight">.
        # selectolax .text() strips the tag and keeps the inner text.
        for hit in hits:
            assert "<span" not in hit.title
            assert "Wasser" in hit.title

    def test_strips_highlight_markers_keeps_text_in_snippet(
        self, search_response_html: str
    ) -> None:
        hits = parse_search_results(search_response_html)
        for hit in hits:
            assert "<span" not in hit.snippet

    def test_url_uses_wp_id(self, search_response_html: str) -> None:
        hits = parse_search_results(search_response_html)
        assert hits[0].url == "https://www.gribs.net/?wp=19880"
        assert hits[2].url == "https://www.gribs.net/?wp=19882"

    def test_retrieved_at_present(self, search_response_html: str) -> None:
        hits = parse_search_results(search_response_html)
        assert hits[0].retrieved_at is not None

    def test_empty_input(self) -> None:
        assert parse_search_results("") == []

    def test_no_results(self) -> None:
        # A bare empty container yields no hits.
        assert parse_search_results("<div class='search-results'></div>") == []

    def test_fallback_to_anchor_format(self) -> None:
        # Older/alternative response shape using <a href="?wp="> anchors.
        # The parser must still work via the fallback path.
        html = (
            "<ul>"
            '<li><a href="https://www.gribs.net/?wp=20001">Altantrag A</a>'
            "<p>Snippet für Altantrag.</p></li>"
            '<li><a href="https://www.gribs.net/?wp=20002">Altantrag B</a></li>'
            "</ul>"
        )
        hits = parse_search_results(html)
        assert len(hits) == 2
        assert hits[0].wp_id == 20001
        assert hits[0].title == "Altantrag A"
        assert hits[0].url == "https://www.gribs.net/?wp=20001"
        # Snippet is the surrounding container text minus the leading title.
        assert "Snippet für Altantrag." in hits[0].snippet

    def test_dedupes_repeated_wp_ids(self) -> None:
        # If the same wp_id appears in multiple .search-result-div entries
        # (e.g. duplicate rows), only the first hit is kept.
        onclick_a = "document.location.href='https://www.gribs.net/?wp=30001'"
        onclick_b = "document.location.href='https://www.gribs.net/?wp=30001'"
        html = (
            "<div class='search-results'>"
            "<div class='search-result-div'>"
            f"<div class='headline' onclick=\"{onclick_a}\">Erster Treffer</div>"
            "<div class='bodyline'>Snippet 1</div>"
            "</div>"
            "<div class='search-result-div'>"
            f"<div class='headline' onclick=\"{onclick_b}\">Duplikat</div>"
            "<div class='bodyline'>Snippet 2</div>"
            "</div>"
            "</div>"
        )
        hits = parse_search_results(html)
        assert len(hits) == 1
        assert hits[0].title == "Erster Treffer"

    def test_skips_result_div_without_wp_id_in_onclick(self) -> None:
        # A .search-result-div whose .headline has no wp_id in onclick is skipped
        # (rather than crashing or producing a hit with wp_id=None).
        onclick = "document.location.href='https://www.gribs.net/some/other/path'"
        html = (
            "<div class='search-results'>"
            "<div class='search-result-div'>"
            f"<div class='headline' onclick=\"{onclick}\">Ohne wp</div>"
            "<div class='bodyline'>Snippet</div>"
            "</div>"
            "</div>"
        )
        hits = parse_search_results(html)
        assert hits == []


class TestParseSinglepost:
    """Tests for `parse_singlepost` against the verified-live JSON shape.

    The response has four keys: `content` (body HTML with .posts-title,
    .post-view-count, hashfield input), `header` (banner with
    .member-banner-title), `views` (recently-viewed list with inlinelink
    carrying the canonical post_id and category path).
    """

    def test_extracts_title_from_posts_title(
        self, singlepost_response_json: dict
    ) -> None:
        post = parse_singlepost(singlepost_response_json)
        assert post.title == "Musterantrag: Trinkwasserschutz im Gemeindegebiet"

    def test_extracts_post_id_from_views_inlinelink(
        self, singlepost_response_json: dict
    ) -> None:
        # The canonical post_id lives in the `views` field's inlinelink(...),
        # NOT just in the content's postWidget() call.
        post = parse_singlepost(singlepost_response_json)
        assert post.post_id == 3102

    def test_extracts_share_hash_from_hashfield(
        self, singlepost_response_json: dict
    ) -> None:
        post = parse_singlepost(singlepost_response_json)
        assert post.share_url == "https://www.gribs.net/?h=77d40ef1fc8c915"

    def test_extracts_view_count_from_post_view_count_span(
        self, singlepost_response_json: dict
    ) -> None:
        # The verified-live format uses <span class="post-view-count">N</span>,
        # NOT 'Views: N' text. The old regex would return None here.
        post = parse_singlepost(singlepost_response_json)
        assert post.view_count == 142

    def test_extracts_breadcrumb_from_header_banner(
        self, singlepost_response_json: dict
    ) -> None:
        # The category name comes from <h1 class="member-banner-title"> in the
        # `header` field (here: "Umwelt").
        post = parse_singlepost(singlepost_response_json)
        assert "Umwelt" in post.category_breadcrumb

    def test_body_html_present(self, singlepost_response_json: dict) -> None:
        post = parse_singlepost(singlepost_response_json)
        assert "Trinkwassers" in post.body_html or "Mustervorlage" in post.body_html

    def test_body_text_trafilatura_extracted(
        self, singlepost_response_json: dict
    ) -> None:
        post = parse_singlepost(singlepost_response_json)
        assert isinstance(post.body_text, str)
        # trafilatura (or selectolax fallback) should yield non-empty plaintext.
        # Bug 3: previously this was empty because trafilatura was called on a
        # bare fragment without a document wrapper.
        assert len(post.body_text) > 100
        assert "<" not in post.body_text  # no HTML tags in plaintext
        # Keywords from the fixture body.
        assert "Kreislaufwirtschaft" in post.body_text
        assert "Erlangen" in post.body_text

    def test_body_text_falls_back_to_selectolax_when_trafilatura_fails(
        self,
    ) -> None:
        # When trafilatura returns None (e.g. for very short/odd fragments),
        # the parser falls back to selectolax .text() so body_text is never
        # empty when body_html has content (Bug 3 Option C).
        payload = {
            "error": False,
            "content": (
                "<div><h1 class='posts-title'>T</h1>"
                "<div class='posts-body'><p>Minimal body text.</p></div></div>"
            ),
            "header": "",
            "views": "",
        }
        post = parse_singlepost(payload)
        assert post.body_text  # non-empty
        assert "Minimal body text" in post.body_text

    def test_url_uses_share_hash(self, singlepost_response_json: dict) -> None:
        post = parse_singlepost(singlepost_response_json)
        # share_url (from hashfield) takes precedence over the post_id fallback.
        assert post.url == "https://www.gribs.net/?h=77d40ef1fc8c915"

    def test_retrieved_at_present(self, singlepost_response_json: dict) -> None:
        post = parse_singlepost(singlepost_response_json)
        assert post.retrieved_at is not None

    def test_post_id_falls_back_to_content_post_widget(self) -> None:
        # When `views` is missing/empty, post_id is inferred from the
        # postWidget() call in `content`.
        payload = {
            "error": False,
            "content": (
                "<div><h1 class='posts-title'>Test</h1>"
                "<script>postWidget(9999, 'share');</script></div>"
            ),
            "header": "",
            "views": "",
        }
        post = parse_singlepost(payload)
        assert post.post_id == 9999

    def test_post_id_none_when_uninferable(self) -> None:
        # No inlinelink in views, no postWidget in content -> post_id is None.
        payload = {
            "error": False,
            "content": "<div><h1 class='posts-title'>Test</h1><p>no widget</p></div>",
            "header": "",
            "views": "",
        }
        post = parse_singlepost(payload)
        assert post.post_id is None

    def test_view_count_none_when_span_absent(self) -> None:
        # No .post-view-count span -> view_count is None (not 0).
        payload = {
            "error": False,
            "content": "<div><h1 class='posts-title'>Test</h1></div>",
            "header": "",
            "views": "",
        }
        post = parse_singlepost(payload)
        assert post.view_count is None

    def test_url_falls_back_to_members_landing_when_no_hash(
        self, singlepost_response_json: dict
    ) -> None:
        # When share_hash is absent and only post_id is known, url falls back
        # to the members landing page (gribs has no ?p=<id> deep-link).
        views_html = (
            '<div onclick="inlinelink({cat:1, l1:7, l2:115, '
            'l3:null, post_id:42})">x</div>'
        )
        payload = {
            "error": False,
            "content": "<div><h1 class='posts-title'>Test</h1></div>",
            "header": "",
            "views": views_html,
        }
        post = parse_singlepost(payload)
        assert post.url == "https://www.gribs.net/members/home"
        assert post.post_id == 42


class TestParseStructure:
    """Tests for `parse_structure`."""

    def test_returns_category_node_with_children(self, structure_html: str) -> None:
        node = parse_structure(structure_html, root_label="Antragsbörse")
        assert node is not None
        assert len(node.children) == 5

    def test_root_label_from_param(self, structure_html: str) -> None:
        node = parse_structure(structure_html, root_label="Antragsbörse")
        assert node.label == "Antragsbörse"

    def test_root_id_from_first_child_cat(self, structure_html: str) -> None:
        # The root id is synthesized from the first child's `cat` value (1).
        node = parse_structure(structure_html, root_label="Antragsbörse")
        assert node.id == 1

    def test_child_labels(self, structure_html: str) -> None:
        node = parse_structure(structure_html, root_label="Antragsbörse")
        labels = [c.label for c in node.children]
        assert labels == [
            "Faire Kommune · Gemeinwohl",
            "Umwelt",
            "Soziales",
            "Finanzen",
            "Bildung",
        ]

    def test_child_ids_from_structexp_l1(self, structure_html: str) -> None:
        # Child ids come from the l1 field of the structexp object. The live
        # format HTML-entity-encodes the object (`{&quot;cat&quot;:&quot;1&quot;,
        # &quot;l1&quot;:&quot;1&quot;}`); the parser must unescape before parsing.
        # Without unescaping, all ids would be 0 (Bug 1).
        node = parse_structure(structure_html, root_label="Antragsbörse")
        ids = [c.id for c in node.children]
        assert ids == [1, 7, 4, 13, 9]

    def test_empty_input(self) -> None:
        node = parse_structure("", root_label="Antragsbörse")
        assert node.children == []
        assert node.label == "Antragsbörse"


class TestParseExpandStructure:
    """Tests for `parse_expand_structure`.

    Note: the verified-live leaf response returns a search form, NOT posts.
    The `.post-widget`-based leaf path here is a legacy/alternative format
    kept for parser robustness.
    """

    def test_leaf_returns_posts(self, expand_structure_leaf_html: str) -> None:
        expansion = parse_expand_structure(expand_structure_leaf_html)
        assert expansion.posts is not None
        assert expansion.subcategories is None
        assert len(expansion.posts) == 2

    def test_leaf_post_ids(self, expand_structure_leaf_html: str) -> None:
        expansion = parse_expand_structure(expand_structure_leaf_html)
        assert expansion.posts is not None
        ids = [p.post_id for p in expansion.posts]
        assert ids == [3102, 3103]

    def test_leaf_post_titles(self, expand_structure_leaf_html: str) -> None:
        expansion = parse_expand_structure(expand_structure_leaf_html)
        assert expansion.posts is not None
        assert expansion.posts[0].title == "Trinkwasserschutz im Gemeindegebiet"

    def test_leaf_post_urls(self, expand_structure_leaf_html: str) -> None:
        expansion = parse_expand_structure(expand_structure_leaf_html)
        assert expansion.posts is not None
        # Only post_id known; gribs has no ?p= deep-link, so falls back to
        # the members landing page.
        assert expansion.posts[0].url == "https://www.gribs.net/members/home"

    def test_intermediate_returns_subcategories(
        self, expand_structure_subcategories_html: str
    ) -> None:
        expansion = parse_expand_structure(expand_structure_subcategories_html)
        assert expansion.subcategories is not None
        assert expansion.posts is None
        assert len(expansion.subcategories) == 3

    def test_intermediate_labels(
        self, expand_structure_subcategories_html: str
    ) -> None:
        expansion = parse_expand_structure(expand_structure_subcategories_html)
        assert expansion.subcategories is not None
        labels = [c.label for c in expansion.subcategories]
        assert labels == ["Abfall", "Artenschutz", "Wasser"]

    def test_intermediate_ids_from_html_encoded_structexp(
        self, expand_structure_subcategories_html: str
    ) -> None:
        # The live format HTML-entity-encodes the structexp object's quotes
        # (`{&quot;cat&quot;:&quot;1&quot;,&quot;l1&quot;:&quot;7&quot;,
        # &quot;l2&quot;:&quot;115&quot;}`). Without unescaping, all ids would
        # be 0 (Bug 1). The deepest non-None id (l2) is used as the node id.
        expansion = parse_expand_structure(expand_structure_subcategories_html)
        assert expansion.subcategories is not None
        ids = [c.id for c in expansion.subcategories]
        assert ids == [115, 120, 125]


class TestParseRecentPosts:
    """Tests for `parse_recent_posts` against the verified-live HTML format.

    Live format: `<div class='post-widget' id='N_pid'>` with
    `<script>postWidget(N,'N_pid','start');</script>` and a spinner — NO titles.
    Titles are fetched separately via `/members/postWidgetFill` (see
    `TestParsePostWidget`).
    """

    def test_returns_list(self, recentposts_html: str) -> None:
        posts = parse_recent_posts(recentposts_html)
        assert len(posts) == 3

    def test_extracts_post_ids_from_post_widget_call(
        self, recentposts_html: str
    ) -> None:
        posts = parse_recent_posts(recentposts_html)
        ids = [p.post_id for p in posts]
        assert ids == [3201, 3202, 3203]

    def test_titles_are_placeholder_when_no_inline_title(
        self, recentposts_html: str
    ) -> None:
        # The scaffold has no title markup — the parser falls back to
        # "Post <id>". Real titles come from parse_post_widget (Bug 2 fix).
        posts = parse_recent_posts(recentposts_html)
        assert posts[0].title == "Post 3201"
        assert posts[1].title == "Post 3202"

    def test_urls_fall_back_to_members_landing(self, recentposts_html: str) -> None:
        # Only post_id is known; gribs.net has no ?p= deep-link, so the url
        # falls back to the members landing page.
        posts = parse_recent_posts(recentposts_html)
        for p in posts:
            assert p.url == "https://www.gribs.net/members/home"
            assert p.retrieved_at is not None

    def test_falls_back_to_id_attr_when_no_post_widget(self) -> None:
        # When the postWidget() call is missing, the parser should still
        # extract post_id from the id='N_pid' attribute.
        html = (
            "<div class='startpage-recent'>"
            "<div class='post-widget' id='42_pid'>"
            "<h3><a href='https://www.gribs.net/?wp=999'>Fallback</a></h3>"
            "</div>"
            "</div>"
        )
        posts = parse_recent_posts(html)
        assert len(posts) == 1
        assert posts[0].post_id == 42
        # When an inline title IS present (legacy format), it's used.
        assert posts[0].title == "Fallback"


class TestParsePostWidget:
    """Tests for `parse_post_widget` against `/members/postWidgetFill` HTML.

    The verified-live response contains `.pwidget-title` (with a `title`
    attribute holding the FULL title — visible text is truncated) and
    `.pwidget-date`. No snippet is returned.
    """

    def test_extracts_full_title_from_title_attr(
        self, postwidgetfill_3103_html: str
    ) -> None:
        teaser = parse_post_widget(postwidgetfill_3103_html, post_id=3103)
        # The FULL title comes from the `title` attribute, NOT the truncated
        # visible text ("Arbeitshilfe zum Berichtsrahmen Nachhaltige Kom...").
        assert teaser.title == (
            "Arbeitshilfe zum Berichtsrahmen Nachhaltige Kommune (BNK)"
        )

    def test_extracts_date(self, postwidgetfill_3103_html: str) -> None:
        teaser = parse_post_widget(postwidgetfill_3103_html, post_id=3103)
        assert teaser.date == "16. 07. 2026"

    def test_post_id_from_arg(self, postwidgetfill_3103_html: str) -> None:
        # post_id is passed in (not parsed from the response).
        teaser = parse_post_widget(postwidgetfill_3103_html, post_id=3103)
        assert teaser.post_id == 3103

    def test_snippet_is_none(self, postwidgetfill_3103_html: str) -> None:
        # postWidgetFill doesn't return a snippet.
        teaser = parse_post_widget(postwidgetfill_3103_html, post_id=3103)
        assert teaser.snippet is None

    def test_url_falls_back_to_members_landing(
        self, postwidgetfill_3103_html: str
    ) -> None:
        teaser = parse_post_widget(postwidgetfill_3103_html, post_id=3103)
        assert teaser.url == "https://www.gribs.net/members/home"

    def test_empty_html_falls_back_to_post_n_title(self) -> None:
        teaser = parse_post_widget("", post_id=999)
        assert teaser.title == "Post 999"
        assert teaser.date is None

    def test_falls_back_to_visible_text_when_no_title_attr(self) -> None:
        # When the .pwidget-title has no `title` attribute, use the visible
        # text (even if truncated).
        html = "<div class='pwidget-title'>Truncated title...</div>"
        teaser = parse_post_widget(html, post_id=1)
        assert teaser.title == "Truncated title..."


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
