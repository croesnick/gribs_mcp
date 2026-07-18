"""HTML/JSON parsers for gribs.net responses.

Pure functions on inputs (no HTTP). selectolax for structured listings,
trafilatura for clean article body text. Parsers are best-effort: missing
fields degrade gracefully to None / empty rather than raising.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

# trafilatura is untyped (see mypy override). Blocking pure-Python extractor.
import trafilatura
from selectolax.parser import HTMLParser

from gribs_mcp.models import (
    CategoryNode,
    PostDetail,
    PostTeaser,
    SearchHit,
    StructureExpansion,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# gribs.net supports `?h=<hash>` and `?wp=<id>` deep-links only.
# There is NO `?p=<post_id>` deep-link on gribs (verified live). When only
# post_id is known and no hash/wp is available, we fall back to the members
# landing page rather than fabricate a broken URL.
MEMBERS_LANDING_URL = "https://www.gribs.net/members/home"


def _gribs_url(
    post_id: int | None = None,  # noqa: ARG001 — kept for API stability
    wp_id: int | None = None,
    hash_: str | None = None,
) -> str:
    """Build a canonical gribs.net deep-link URL.

    Prefers hash > wp_id (per INTENT.md §ID-Dualität). When neither is known,
    falls back to the members landing page — gribs.net has no `?p=<post_id>`
    deep-link, so fabricating one would yield a broken URL.
    """
    if hash_:
        return f"https://www.gribs.net/?h={hash_}"
    if wp_id is not None:
        return f"https://www.gribs.net/?wp={wp_id}"
    return MEMBERS_LANDING_URL


def _extract_wp_id_from_href(href: str | None) -> int | None:
    """Extract `wp` query param from a URL, returning it as int or None."""
    if not href:
        return None
    qs = parse_qs(urlsplit(href).query)
    values = qs.get("wp")
    if not values:
        return None
    try:
        return int(values[0])
    except ValueError:
        return None


_WP_RE = re.compile(r"[?&]wp=(\d+)")
_HASH_RE = re.compile(r"[?&]h=([0-9a-fA-F]+)")
_POST_ID_RE = re.compile(r"postWidget\(\s*(\d+)")


def _extract_wp_id(text: str | None) -> int | None:
    """Extract `wp` id from any string containing `?wp=<id>` or `&wp=<id>`.

    Handles both plain hrefs (`href="...?wp=19880"`) and onclick handlers
    (`onclick="document.location.href='...?wp=19880'"`).
    """
    if not text:
        return None
    match = _WP_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def parse_search_results(html: str) -> list[SearchHit]:
    """Parse `/members/search` HTML response into SearchHit list.

    Supports two response formats:

    1. **Verified live format** (primary): `.search-result-div` containers,
       each with a `.headline` div carrying an
       `onclick="document.location.href='...?wp=<id>'"` handler, and a
       `.bodyline` div with the snippet text. NO `<a href>` tags are present.
    2. **Anchor format** (fallback): rows containing `<a href="...?wp=<id>">`
       anchors — kept for older/alternative response shapes.

    In both cases, `<span class="highlight">` markers in title/snippet are
    stripped while preserving their inner text.

    Args:
        html: HTML snippet returned in the `content` field of the JSON response.

    Returns:
        List of SearchHit objects (empty if no results found).
    """
    if not html:
        return []

    tree = HTMLParser(html)
    hits: list[SearchHit] = []
    now = _utcnow()
    seen_wp: set[int] = set()

    # Primary: verified live format using .search-result-div containers.
    for result_div in tree.css(".search-result-div"):
        headline = result_div.css_first(".headline")
        if headline is None:
            continue

        # wp_id lives in the onclick="document.location.href='...?wp=<id>'" attr.
        onclick = headline.attributes.get("onclick", "")
        wp_id = _extract_wp_id(onclick)
        if wp_id is None or wp_id in seen_wp:
            continue

        title = (headline.text() or "").strip()
        if not title:
            continue
        seen_wp.add(wp_id)

        snippet = ""
        bodyline = result_div.css_first(".bodyline")
        if bodyline is not None:
            snippet = (bodyline.text() or "").strip()

        hits.append(
            SearchHit(
                title=title,
                snippet=snippet,
                wp_id=wp_id,
                url=_gribs_url(wp_id=wp_id),
                retrieved_at=now,
            )
        )

    if hits:
        return hits

    # Fallback: anchor-based format (older/alternative responses).
    for a in tree.css("a[href]"):
        href = a.attributes.get("href", "")
        wp_id = _extract_wp_id_from_href(href)
        if wp_id is None or wp_id in seen_wp:
            continue
        seen_wp.add(wp_id)

        title = (a.text() or "").strip()
        if not title:
            continue

        # Climb to nearest container for snippet text.
        container = a.parent
        snippet = ""
        if container is not None:
            # selectolax `.text()` strips tags but preserves inner text, so
            # `<span class="highlight">x</span>` becomes just "x".
            snippet_text = (container.text() or "").strip()
            if snippet_text.startswith(title):
                snippet_text = snippet_text[len(title) :].strip()
            snippet = snippet_text

        hits.append(
            SearchHit(
                title=title,
                snippet=snippet,
                wp_id=wp_id,
                url=_gribs_url(wp_id=wp_id),
                retrieved_at=now,
            )
        )

    return hits


def _extract_share_hash(html: str) -> str | None:
    match = _HASH_RE.search(html)
    return match.group(1) if match else None


# Matches `<span class="post-view-count">N</span>` (verified live format).
_VIEW_COUNT_SPAN_RE = re.compile(r"post-view-count[^>]*>\s*(\d+)\s*<", re.IGNORECASE)

# Matches `id="hashfield_<id>" value="https://www.gribs.net/?h=<hash>"`.
_HASHFIELD_RE = re.compile(
    r'id="hashfield_\d+"[^>]*value="https://www\.gribs\.net/\?h=([0-9a-fA-F]+)"'
)


def _extract_view_count(content_html: str) -> int | None:
    """Extract view count from `<span class="post-view-count">N</span>`.

    The verified live format uses this dedicated span; the old 'Views: N' /
    'Aufrufe' text regex no longer matches and has been removed.
    """
    match = _VIEW_COUNT_SPAN_RE.search(content_html)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _extract_share_hash_from_hashfield(content_html: str) -> str | None:
    """Extract share hash from `<input id="hashfield_<id>" value="...?h=<hash>">."""
    match = _HASHFIELD_RE.search(content_html)
    return match.group(1) if match else None


def _extract_post_id_from_views(views_html: str) -> int | None:
    """Extract post_id from the first `inlinelink({..., post_id:N})` in `views`.

    The `views` field of `/members/singlepost` is an HTML list of
    recently-viewed favorites. The first entry's `inlinelink(...)` carries the
    canonical post_id for THIS post (plus its full category path).
    """
    obj = _parse_inlinelink_object(views_html)
    if obj is None:
        return None
    raw_post_id = obj.get("post_id")
    if raw_post_id is None:
        return None
    try:
        return int(raw_post_id)
    except (TypeError, ValueError):
        return None


def _extract_category_path_from_views(views_html: str) -> dict[str, int | None]:
    """Extract cat/l1/l2/l3 ids from the first `inlinelink({...})` in `views`.

    Returns a dict with keys 'cat', 'l1', 'l2', 'l3' (values None if absent).
    Useful for breadcrumb enrichment when the `header` field lacks them.
    """
    obj = _parse_inlinelink_object(views_html)
    if obj is None:
        return {"cat": None, "l1": None, "l2": None, "l3": None}

    def _as_int(v: Any) -> int | None:
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return {
        "cat": _as_int(obj.get("cat")),
        "l1": _as_int(obj.get("l1")),
        "l2": _as_int(obj.get("l2")),
        "l3": _as_int(obj.get("l3")),
    }


# Matches `inlinelink({…})` — the object body is HTML-entity-encoded in live
# responses (`{&quot;cat&quot;:1,&quot;post_id&quot;:3102}`).
_INLINELINK_RE = re.compile(r"inlinelink\(\s*(\{[^}]*\})\s*\)", re.DOTALL)


def _parse_inlinelink_object(html_str: str) -> dict[str, Any] | None:
    """Parse the first `inlinelink({…})` JS object from an HTML string.

    The live `views` field HTML-encodes quotes as `&quot;`, so the object body
    looks like `{&quot;cat&quot;:&quot;1&quot;,&quot;l1&quot;:&quot;7&quot;,...}`.
    Delegates to :func:`_parse_js_object` which unescapes then parses.
    """
    return _parse_js_object(html_str, _INLINELINK_RE)


def _extract_banner_title(header_html: str) -> str | None:
    """Extract the category banner title from `<h1 class="member-banner-title">`.

    The `header` field of `/members/singlepost` contains this banner whose
    text is the post's top-level category name (e.g. "Umwelt"). Used as the
    primary breadcrumb source.
    """
    if not header_html:
        return None
    tree = HTMLParser(header_html)
    node = tree.css_first(".member-banner-title, h1.member-banner-title")
    if node is None:
        return None
    text = (node.text() or "").strip()
    return text or None


def _strip_tags(html_fragment: str) -> str:
    """Cheap tag stripper for breadcrumb text."""
    return re.sub(r"<[^>]+>", "", html_fragment).strip()


def _parse_breadcrumb(tree: HTMLParser) -> list[str]:
    """Extract breadcrumb path. Looks for `.breadcrumb` list items.

    Returns an empty list when no breadcrumb is found — this is valid and
    means the post had no parseable breadcrumb in its `content` field.
    """
    items = tree.css(".breadcrumb li")
    if items:
        return [(i.text() or "").strip() for i in items if (i.text() or "").strip()]
    # Fallback: any element with breadcrumb-ish class.
    items = tree.css("[class*=breadcrumb] a, [class*=Breadcrumb] a")
    return [(i.text() or "").strip() for i in items if (i.text() or "").strip()]


def _extract_body_html(tree: HTMLParser) -> str:
    """Locate the main post body HTML.

    Selectors tried in order:
    - `.posts-body` (verified-live format for `/members/singlepost` content).
    - `.single-post-content` / `.post-content` / `.entry-content` (fallbacks).
    - `article` (last resort).
    """
    for selector in (
        ".posts-body",
        ".single-post-content",
        ".post-content",
        ".entry-content",
        "article",
    ):
        node = tree.css_first(selector)
        if node is not None:
            return node.html or ""
    return ""


def parse_singlepost(json_response: dict[str, Any]) -> PostDetail:
    """Parse `/members/singlepost` JSON response into PostDetail.

    The verified-live response has four string keys (all HTML except `error`):
    - `content`: post body HTML with `.posts-title`, `.posts-date`,
      `<span class="post-view-count">N</span>`, and the
      `<input id="hashfield_<id>" value="...?h=<hash>">` share field.
    - `header`: category banner with `<h1 class="member-banner-title">…</h1>`.
    - `views`: HTML list of recently-viewed favorites; the first
      `inlinelink({cat, l1, l2, l3, post_id})` carries THIS post's canonical
      post_id and full category path.

    Args:
        json_response: Parsed JSON dict from the API.

    Returns:
        PostDetail with all fields populated where extractable. `post_id`
        is None if inference from both `content` and `views` fails.
    """
    content = json_response.get("content", "")
    if not isinstance(content, str):
        content = ""
    header = json_response.get("header", "")
    if not isinstance(header, str):
        header = ""
    views = json_response.get("views", "")
    if not isinstance(views, str):
        views = ""

    # post_id: prefer the canonical id from `views` (inlinelink); fall back to
    # the postWidget() call in `content`.
    post_id = _extract_post_id_from_views(views)
    if post_id is None:
        post_id = _infer_post_id_from_content(content)

    tree = HTMLParser(content)

    # Title: prefer .posts-title (verified live), then generic fallbacks.
    title = ""
    for selector in (
        ".posts-title",
        "h1.post-title",
        "h1",
        "h2.post-title",
        "h2",
        ".post-title",
    ):
        node = tree.css_first(selector)
        text = (node.text() or "").strip() if node is not None else ""
        if text:
            title = text
            break

    # Date: any element with class containing 'date'.
    date: str | None = None
    node = tree.css_first("[class*=date], [class*=Date], time")
    if node is not None:
        date = (node.text() or "").strip() or None

    # View count: <span class="post-view-count">N</span> (verified live).
    view_count = _extract_view_count(content)

    # Share hash: prefer the dedicated hashfield input, fall back to any ?h=.
    share_hash = _extract_share_hash_from_hashfield(content)
    if share_hash is None:
        share_hash = _extract_share_hash(content)
    share_url = f"https://www.gribs.net/?h={share_hash}" if share_hash else None

    # Breadcrumb: prefer the banner title from `header`; fall back to the
    # `.breadcrumb` list in `content` if present.
    breadcrumb: list[str] = []
    banner_title = _extract_banner_title(header)
    if banner_title:
        breadcrumb.append(banner_title)
    content_breadcrumb = _parse_breadcrumb(tree)
    if content_breadcrumb and (
        not breadcrumb or breadcrumb[0] != content_breadcrumb[0]
    ):
        # Avoid duplicating the banner title if it's already the first entry.
        breadcrumb.extend(content_breadcrumb)

    body_html = _extract_body_html(tree)

    # Body text: try trafilatura on a full-document wrapper first (it's
    # designed for full HTML documents, not fragments — passing a bare
    # fragment returns None). If trafilatura returns None/empty, fall back
    # to selectolax's HTMLParser.text() which strips tags and normalizes
    # whitespace. This ensures body_text is never empty when body_html has
    # content (Bug 3 fix).
    body_text = ""
    if body_html:
        doc = (
            body_html
            if "<html" in body_html.lower()
            else f"<html><body>{body_html}</body></html>"
        )
        extracted = trafilatura.extract(
            doc, include_comments=False, include_tables=False
        )
        body_text = (extracted or "").strip()
        if not body_text:
            # Fallback: selectolax tag-strip + whitespace normalize.
            body_tree = HTMLParser(body_html)
            body_text = (body_tree.text() or "").strip()

    url = share_url or _gribs_url(post_id=post_id)
    return PostDetail(
        post_id=post_id,
        title=title,
        date=date,
        view_count=view_count,
        share_url=share_url,
        category_breadcrumb=breadcrumb,
        body_html=body_html,
        body_text=body_text,
        url=url,
        retrieved_at=_utcnow(),
    )


def _infer_post_id_from_content(content_html: str) -> int | None:
    """Best-effort: extract post_id from a `postWidget(<id>, ...)` call.

    Returns None if no match is found. The canonical post_id lives in the
    `views` field (see `_extract_post_id_from_views`); this helper is a
    fallback for responses missing `views`.
    """
    if not isinstance(content_html, str):
        return None
    match = _POST_ID_RE.search(content_html)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _parse_struct_label(node: Any) -> str:
    """Extract label text from a `.menu-struct-label` node."""
    text = node.text() if hasattr(node, "text") else ""
    return (text or "").strip()


# Matches `structexp('id',{...},'idsuff','level','type')`. The object body is
# HTML-entity-encoded in the live response (`{&quot;cat&quot;:&quot;1&quot;}`).
_STRUCTEXP_RE = re.compile(
    r"structexp\(\s*'[^']*'\s*,\s*(\{[^}]*\})\s*,\s*'[^']*'\s*,\s*'[^']*'\s*,\s*'[^']*'\s*\)",
    re.DOTALL,
)
# Legacy fallback: `structexp(1, 7, 12, null)` positional args.
_STRUCTEXP_LEGACY_RE = re.compile(r"structexp\(([^)]*)\)")


def _parse_js_object(html_str: str, pattern: re.Pattern[str]) -> dict[str, Any] | None:
    """Extract and parse a JS object literal from an HTML string.

    Handles the verified-live format where the object's quotes are HTML-entity
    encoded (`{&quot;cat&quot;:&quot;1&quot;}`): we unescape HTML entities first,
    then quote any still-unquoted keys, then json.loads.

    Args:
        html_str: The raw HTML string (e.g. an onclick attribute value).
        pattern: Compiled regex with one capturing group for the object body.

    Returns:
        Parsed dict, or None if no match / parse failure.
    """
    match = pattern.search(html_str)
    if not match:
        return None
    obj_str = html.unescape(match.group(1))
    # Quote unquoted JS keys: `{cat:1}` -> `{"cat":1}`. Skips already-quoted keys.
    quoted = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', obj_str)
    try:
        obj: dict[str, Any] = json.loads(quoted)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj


def _parse_structexp_args(
    node: Any,
) -> tuple[int, int | None, int | None, int | None] | None:
    """Extract cat / l1 / l2 / l3 ids from a node containing `structexp(...)`.

    Handles two formats:

    1. **Verified live** (HTML-entity-encoded object):
       `structexp('m_l1_1',{&quot;cat&quot;:&quot;1&quot;,&quot;l1&quot;:&quot;1&quot;},'1_1','l1','load')`
       — the object's quotes are HTML-encoded; we unescape then parse.

    2. **Legacy positional**: `structexp(1, 7, 12, null)` — positional ints.

    Returns (cat, l1, l2, l3) with None for missing. Returns None if no
    `structexp(...)` call is found at all.
    """
    onclick = node.attributes.get("onclick", "") if hasattr(node, "attributes") else ""
    if not onclick:
        # Also scan child elements (the label may be inside the clickable).
        for child in node.iter() if hasattr(node, "iter") else []:
            onclick = (
                child.attributes.get("onclick", "")
                if hasattr(child, "attributes")
                else ""
            )
            if onclick:
                break
    if not onclick:
        return None

    def _as_int(v: Any) -> int | None:
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    # Primary: verified-live object format.
    obj = _parse_js_object(onclick, _STRUCTEXP_RE)
    if obj is not None:
        cat = _as_int(obj.get("cat")) or 0
        l1 = _as_int(obj.get("l1"))
        l2 = _as_int(obj.get("l2"))
        l3 = _as_int(obj.get("l3"))
        return cat, l1, l2, l3

    # Legacy fallback: positional args `structexp(cat, l1, l2, l3)`.
    legacy_match = _STRUCTEXP_LEGACY_RE.search(onclick)
    if not legacy_match:
        return None
    args_str = legacy_match.group(1)
    # Skip the object format (handled above) — only parse pure positional.
    if "{" in args_str:
        return None
    args: list[int | None] = []
    for raw in args_str.split(","):
        raw = raw.strip()
        # Strip surrounding quotes (e.g. 'm_l1_1' is not an int).
        if raw.startswith(("'", '"')) and raw.endswith(("'", '"')):
            args.append(None)
            continue
        if not raw or raw.lower() in {"null", "none", "undefined"}:
            args.append(None)
            continue
        try:
            args.append(int(raw))
        except ValueError:
            args.append(None)
    while len(args) < 4:
        args.append(None)
    cat = args[0] if args[0] is not None else 0
    return cat, args[1], args[2], args[3]


def parse_structure(html: str, root_label: str = "") -> CategoryNode:
    """Parse `/members/structure` HTML into a recursive CategoryNode tree.

    The `navigation` field (NOT `content`) of the structure response contains
    `.menu-struct-label` divs with `onclick` handlers calling
    `structexp(cat, l1, l2, l3)` for navigation.

    The synthesized root node's `id` is taken from the first child's `cat`
    value (the top-level category id, e.g. 1 for Antragsbörse). The root label
    is the caller-supplied `root_label` (the section name) — the response HTML
    does not echo the section name back.

    Args:
        html: HTML snippet from the `navigation` field of the JSON response.
        root_label: Human-readable label for the synthesized root node
            (e.g. "Antragsbörse"). Empty string if unknown.

    Returns:
        A CategoryNode representing the top-level category with its L1 children.
    """
    if not html:
        return CategoryNode(id=0, label=root_label, children=[])

    tree = HTMLParser(html)
    children: list[CategoryNode] = []
    root_cat_id = 0

    for label_node in tree.css(".menu-struct-label"):
        label = _parse_struct_label(label_node)
        if not label:
            continue
        args = _parse_structexp_args(label_node)
        if args is None:
            continue
        cat, l1, _l2, _l3 = args
        # Capture the top-level category id from the first child we see.
        if root_cat_id == 0 and cat:
            root_cat_id = cat
        # Use l1 as the node id (these are L1 entries); fall back to cat.
        node_id = l1 if l1 is not None else cat
        children.append(CategoryNode(id=node_id, label=label, children=[]))

    return CategoryNode(id=root_cat_id, label=root_label, children=children)


def parse_expand_structure(html: str) -> StructureExpansion:
    """Parse `/members/expandStructure` HTML into either subcategories or posts.

    - Intermediate nodes return a list of `.menu-struct-label` subcategories.
    - Leaf nodes return a list of `.post-widget` containers with `post_id`
      extractable from `postWidget(post_id, ...)` calls.

    Args:
        html: HTML snippet returned in the `content` field of the JSON response.

    Returns:
        StructureExpansion with `subcategories` (intermediate) or `posts` (leaf)
        populated.
    """
    if not html:
        return StructureExpansion(subcategories=[], posts=None)

    tree = HTMLParser(html)
    now = _utcnow()

    # Leaf nodes: .post-widget containers.
    post_widgets = tree.css(".post-widget")
    if post_widgets:
        posts: list[PostTeaser] = []
        seen: set[int] = set()
        for widget in post_widgets:
            widget_html = widget.html or ""
            match = _POST_ID_RE.search(widget_html)
            if not match:
                continue
            try:
                post_id = int(match.group(1))
            except ValueError:
                continue
            if post_id in seen:
                continue
            seen.add(post_id)

            title = ""
            title_node = widget.css_first("a, .post-title, h3, h4")
            if title_node is not None:
                title = (title_node.text() or "").strip()

            date: str | None = None
            date_node = widget.css_first("[class*=date], time")
            if date_node is not None:
                date = (date_node.text() or "").strip() or None

            snippet: str | None = None
            snippet_node = widget.css_first(".post-snippet, .snippet, p")
            if snippet_node is not None:
                snippet = (snippet_node.text() or "").strip() or None

            posts.append(
                PostTeaser(
                    post_id=post_id,
                    title=title or f"Post {post_id}",
                    date=date,
                    snippet=snippet,
                    url=_gribs_url(post_id=post_id),
                    retrieved_at=now,
                )
            )
        if posts:
            return StructureExpansion(subcategories=None, posts=posts)

    # Intermediate nodes: .menu-struct-label subcategories.
    subcategories: list[CategoryNode] = []
    for label_node in tree.css(".menu-struct-label"):
        label = _parse_struct_label(label_node)
        if not label:
            continue
        args = _parse_structexp_args(label_node)
        node_id = 0
        if args:
            # Prefer the deepest non-None id (l3 > l2 > l1 > cat).
            for v in reversed(args):
                if v is not None:
                    node_id = v
                    break
        subcategories.append(CategoryNode(id=node_id, label=label, children=[]))

    return StructureExpansion(subcategories=subcategories or None, posts=None)


def parse_recent_posts(html: str) -> list[PostTeaser]:
    """Parse `/post/recentposts` HTML into a list of PostTeaser.

    Verified-live format (public endpoint, raw HTML — no JSON wrapper):

        <div class='startpage-recent'>
          <div class='post-widget' id='3103_pid'>
            <script>postWidget(3103,'3103_pid','members');</script>
            ...title/date/snippet markup...
          </div>
          ...
        </div>

    The post_id appears in TWO places per widget: the `id='N_pid'` attribute
    and the `postWidget(N, ...)` call. We extract from the `postWidget()` call
    (canonical) and fall back to the `id` attribute, then to `?wp=` hrefs for
    older formats.
    """
    if not html:
        return []

    tree = HTMLParser(html)
    now = _utcnow()
    posts: list[PostTeaser] = []
    seen: set[int] = set()

    # Match `id='N_pid'` or `id="N_pid"` on post-widget containers.
    _PID_ATTR_RE = re.compile(r"^(\d+)_pid$")

    for container in tree.css(".post-widget, .recent-post, article"):
        widget_html = container.html or ""
        post_id: int | None = None

        # Primary: postWidget(N, ...) call (verified live format).
        match = _POST_ID_RE.search(widget_html)
        if match:
            try:
                post_id = int(match.group(1))
            except ValueError:
                post_id = None

        # Fallback 1: id='N_pid' attribute on the container itself.
        if post_id is None:
            widget_id = container.attributes.get("id", "") or ""
            id_match = _PID_ATTR_RE.match(widget_id)
            if id_match:
                try:
                    post_id = int(id_match.group(1))
                except ValueError:
                    post_id = None

        # Fallback 2: ?wp=<id> in hrefs (older formats).
        if post_id is None:
            for a in container.css("a[href]"):
                href = a.attributes.get("href", "")
                wp = _extract_wp_id_from_href(href)
                if wp is not None:
                    post_id = wp
                    break

        if post_id is None or post_id in seen:
            continue
        seen.add(post_id)

        title_node = container.css_first("a, h3, h4, .post-title")
        title = (title_node.text() or "").strip() if title_node is not None else ""

        date_node = container.css_first("[class*=date], time")
        date = (date_node.text() or "").strip() if date_node is not None else None

        snippet_node = container.css_first("p, .snippet")
        snippet = (
            (snippet_node.text() or "").strip() if snippet_node is not None else None
        )

        posts.append(
            PostTeaser(
                post_id=post_id,
                title=title or f"Post {post_id}",
                date=date or None,
                snippet=snippet or None,
                # Only post_id is known here; gribs.net has no ?p= deep-link,
                # so fall back to the members landing page.
                url=_gribs_url(post_id=post_id),
                retrieved_at=now,
            )
        )

    return posts


def parse_post_widget(html: str, post_id: int) -> PostTeaser:
    """Parse `/members/postWidgetFill` HTML into a PostTeaser.

    The verified-live response contains:
    - `.pwidget-title` with a `title` attribute holding the FULL title (the
      visible text is truncated with "..." suffix). We use the `title` attr.
    - `.pwidget-date` with the display date.
    - No snippet in this response — `snippet` is set to None.

    Args:
        html: Raw HTML from `/members/postWidgetFill`.
        post_id: The post_id used in the fill request (not echoed in response).

    Returns:
        PostTeaser with title (full, from `title` attr), date, and members
        landing URL fallback (no hash/wp available from widget alone).
    """
    now = _utcnow()
    title = ""
    date: str | None = None

    if html:
        tree = HTMLParser(html)
        title_node = tree.css_first(".pwidget-title")
        if title_node is not None:
            # Prefer the full title from the `title` attribute (visible text
            # is truncated with "..." in the live format).
            title = (title_node.attributes.get("title", "") or "").strip()
            if not title:
                title = (title_node.text() or "").strip()

        date_node = tree.css_first(".pwidget-date")
        if date_node is not None:
            date = (date_node.text() or "").strip() or None

    return PostTeaser(
        post_id=post_id,
        title=title or f"Post {post_id}",
        date=date,
        snippet=None,  # postWidgetFill doesn't return a snippet.
        url=_gribs_url(post_id=post_id),
        retrieved_at=now,
    )


async def parse_singlepost_async(json_response: dict[str, Any]) -> PostDetail:
    """Wrap parse_singlepost for callers wanting to offload trafilatura's blocking work.

    trafilatura is CPU-bound and synchronous. Use this in client code that runs
    inside an event loop.
    """
    return await asyncio.to_thread(parse_singlepost, json_response)


__all__ = [
    "MEMBERS_LANDING_URL",
    "parse_expand_structure",
    "parse_post_widget",
    "parse_recent_posts",
    "parse_search_results",
    "parse_singlepost",
    "parse_singlepost_async",
    "parse_structure",
]
