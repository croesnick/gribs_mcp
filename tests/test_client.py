"""Client tests with mocked httpx transport (no live HTTP, no real keyring).

Covers:
- JSON vs HTML response handling (L1).
- 401-retry path (C7).
- Stale cookies trigger login (I5).
- Login success / failure.
- `async with GribsClient()` context manager (C1).
- `GribsAuthError` subclasses `auth.AuthError` (C6).
- search HTML parsing end-to-end through the client.
- recentposts HTML parsing end-to-end (no auth).
- malformed JSON raises GribsApiError.
- empty `content` field handling.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from gribs_mcp import auth
from gribs_mcp.client import (
    GribsApiError,
    GribsAuthError,
    GribsClient,
)

# ---------------------------------------------------------------------------
# Fixtures local to client tests (don't touch the real OS keyring).
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_credentials() -> auth.Credentials:
    return auth.Credentials(email="tester@example.org", password="hunter2")


@pytest.fixture
def patched_keyring(
    monkeypatch: pytest.MonkeyPatch, fake_credentials: auth.Credentials
):
    """Patch auth module so no real keyring access happens.

    Cookies start empty (forcing login); store_cookies captures into a list
    so tests can assert on what got persisted. delete_cookies clears that
    list so the 401-retry path's `load_cookies()` check sees None and logs in.
    """
    stored_cookies: list[auth.CookieEntry] = []

    def _load_credentials() -> auth.Credentials:
        return fake_credentials

    def _load_cookies() -> list[auth.CookieEntry] | None:
        # Returns whatever was last stored (or None if empty) — mirrors the
        # real keyring-backed behavior.
        return list(stored_cookies) if stored_cookies else None

    def _store_cookies(cookies: list[auth.CookieEntry]) -> None:
        stored_cookies.clear()
        stored_cookies.extend(cookies)

    def _delete_cookies() -> None:
        stored_cookies.clear()

    monkeypatch.setattr(auth, "load_credentials", _load_credentials)
    monkeypatch.setattr(auth, "load_cookies", _load_cookies)
    monkeypatch.setattr(auth, "store_cookies", _store_cookies)
    monkeypatch.setattr(auth, "delete_cookies", _delete_cookies)
    return stored_cookies


def _make_client(
    handler: httpx.MockTransport.handler_type,
    *,
    base_url: str = "https://www.gribs.net",
) -> GribsClient:
    """Build a GribsClient whose httpx.AsyncClient uses a MockTransport.

    We bypass `_ensure_client`'s real AsyncClient construction by pre-seeding
    a client with the mock transport.
    """
    client = GribsClient(base_url=base_url)
    client._client = httpx.AsyncClient(
        base_url=base_url,
        transport=httpx.MockTransport(handler),
        cookies=httpx.Cookies(),
    )
    return client


def _set_cookie_handler(
    cookie_name: str = "PHPSESSID",
    cookie_value: str = "abc123",
) -> httpx.MockTransport.handler_type:
    """A login handler that sets a session cookie and returns success."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/ajax_login":
            resp = httpx.Response(
                200,
                text="1",
                headers={
                    "Set-Cookie": f"{cookie_name}={cookie_value}; Path=/; HttpOnly",
                    "Content-Type": "text/html; charset=UTF-8",
                },
            )
            return resp
        return httpx.Response(404, text="not found")

    return handler


# ---------------------------------------------------------------------------
# C1: context manager
# ---------------------------------------------------------------------------


class TestContextManager:
    async def test_async_with_opens_and_closes(self, patched_keyring) -> None:
        handler = _set_cookie_handler()
        client = _make_client(handler)
        async with client as c:
            assert c is client
            await c.login()
        # After exit, the underlying client is closed.
        assert client._client is None


# ---------------------------------------------------------------------------
# C6: error subclass relationship
# ---------------------------------------------------------------------------


class TestErrorHierarchy:
    def test_gribs_auth_error_subclasses_auth_error(self) -> None:
        assert issubclass(GribsAuthError, auth.AuthError)

    def test_catching_auth_error_catches_gribs_auth_error(self) -> None:
        try:
            raise GribsAuthError("test")
        except auth.AuthError as exc:
            assert "test" in str(exc)
        else:
            pytest.fail("GribsAuthError should be caught by except AuthError")


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------


class TestLogin:
    async def test_login_success_sets_cookie_and_persists(
        self, patched_keyring, fake_credentials
    ) -> None:
        handler = _set_cookie_handler()
        client = _make_client(handler)
        await client.login()
        # Cookie jar has the session cookie.
        assert client._client is not None
        assert "PHPSESSID" in client._client.cookies
        # store_cookies was called with the cookie.
        assert patched_keyring, "expected at least one persisted cookie"
        names = [c.get("name") for c in patched_keyring]
        assert "PHPSESSID" in names

    async def test_login_failure_no_cookie_raises(self, patched_keyring) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="bad")

        client = _make_client(handler)
        with pytest.raises(GribsAuthError, match="did not set a session cookie"):
            await client.login()

    async def test_login_http_error_raises(self, patched_keyring) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="server error")

        client = _make_client(handler)
        with pytest.raises(GribsAuthError, match="Login HTTP 500"):
            await client.login()


# ---------------------------------------------------------------------------
# L1: JSON vs HTML response handling
# ---------------------------------------------------------------------------


class TestResponseFormats:
    async def test_search_returns_html_and_parses(self, patched_keyring) -> None:
        onclick = "document.location.href='https://www.gribs.net/?wp=100'"
        search_html = (
            "<div class='search-results'>"
            "<div class='search-result-div'>"
            f"<div class='headline' onclick=\"{onclick}\">Hit One</div>"
            "<div class='bodyline'>snippet one</div>"
            "</div>"
            "</div>"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/users/ajax_login":
                return httpx.Response(
                    200,
                    text="1",
                    headers={"Set-Cookie": "PHPSESSID=x; Path=/"},
                )
            if request.url.path == "/members/search":
                # Raw HTML, NOT JSON.
                return httpx.Response(
                    200, text=search_html, headers={"Content-Type": "text/html"}
                )
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        hits = await client.search(query="test", category_id=1)
        assert len(hits) == 1
        assert hits[0].wp_id == 100
        assert hits[0].title == "Hit One"

    async def test_recentposts_returns_html_no_auth(self, patched_keyring) -> None:
        # The scaffold has no titles; recent_posts must call postWidgetFill
        # for each post to enrich (Bug 2 fix). This is the N+1 pattern.
        recent_html = (
            "<div class='startpage-recent'>"
            "<div class='post-widget' id='500_pid'>"
            "<script>postWidget(500,'500_pid','start');</script>"
            "</div>"
            "</div>"
        )
        widget_html = (
            "<div class='pwidget-date'>01. 01. 2024</div>"
            "<div class='pwidget-title' title='Enriched Title'>Enriched...</div>"
        )

        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if request.url.path == "/post/recentposts":
                return httpx.Response(
                    200, text=recent_html, headers={"Content-Type": "text/html"}
                )
            if request.url.path == "/members/postWidgetFill":
                return httpx.Response(
                    200, text=widget_html, headers={"Content-Type": "text/html"}
                )
            # Any other path (including /users/ajax_login) is an error —
            # recent_posts must NOT trigger auth.
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        posts = await client.recent_posts()
        assert len(posts) == 1
        assert posts[0].post_id == 500
        # Title comes from the postWidgetFill response's `title` attr.
        assert posts[0].title == "Enriched Title"
        assert posts[0].date == "01. 01. 2024"
        # 2 calls: 1 scaffold + 1 fill (no login).
        assert call_count == 2

    async def test_recentposts_enrichment_failure_keeps_scaffold(
        self, patched_keyring
    ) -> None:
        # If postWidgetFill fails for one post, the scaffold (title="Post N")
        # is kept rather than dropping the post entirely.
        recent_html = (
            "<div class='startpage-recent'>"
            "<div class='post-widget' id='501_pid'>"
            "<script>postWidget(501,'501_pid','start');</script>"
            "</div>"
            "</div>"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/post/recentposts":
                return httpx.Response(
                    200, text=recent_html, headers={"Content-Type": "text/html"}
                )
            if request.url.path == "/members/postWidgetFill":
                return httpx.Response(500, text="server error")
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        posts = await client.recent_posts()
        assert len(posts) == 1
        assert posts[0].post_id == 501
        # Enrichment failed, so the scaffold title ("Post 501") is kept.
        assert posts[0].title == "Post 501"

    async def test_structure_reads_navigation_key(self, patched_keyring) -> None:
        navigation_html = (
            "<div class='menu-struct'>"
            "<div class='menu-struct-label' onclick='structexp(1, 7, null, null)'>"
            "Umwelt</div>"
            "</div>"
        )
        payload = {
            "error": False,
            "navigation": navigation_html,
            "content": "<div class='green-grid'>not the tree</div>",
            "properties": "",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/users/ajax_login":
                return httpx.Response(
                    200,
                    text="1",
                    headers={"Set-Cookie": "PHPSESSID=x; Path=/"},
                )
            if request.url.path == "/members/structure":
                return httpx.Response(
                    200,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        node = await client.list_categories(category_id=1, root_label="Antragsbörse")
        # The navigation HTML was parsed (NOT the green-grid content).
        assert node.label == "Antragsbörse"
        assert len(node.children) == 1
        assert node.children[0].label == "Umwelt"
        assert node.children[0].id == 7
        assert node.id == 1  # root id from first child's cat

    async def test_structure_parses_html_encoded_structexp(
        self, patched_keyring
    ) -> None:
        # Verified-live format: structexp object quotes are HTML-entity-encoded
        # as &quot;. Without unescaping, all child ids would be 0 (Bug 1).
        nav_html = (
            "<div class='menu-struct'>"
            "<div id='m_l1_1' class='menu-struct-label' "
            'onclick="structexp(&#039;m_l1_1&#039;,{&quot;cat&quot;:&quot;1&quot;,'
            '&quot;l1&quot;:&quot;1&quot;},&#039;1_1&#039;,&#039;l1&#039;,&#039;load&#039;);">'
            "Faire Kommune</div>"
            "</div>"
        )
        payload = {
            "error": False,
            "navigation": nav_html,
            "content": "",
            "properties": "",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/users/ajax_login":
                return httpx.Response(
                    200,
                    text="1",
                    headers={"Set-Cookie": "PHPSESSID=x; Path=/"},
                )
            if request.url.path == "/members/structure":
                return httpx.Response(200, json=payload)
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        node = await client.list_categories(category_id=1, root_label="Antragsbörse")
        assert len(node.children) == 1
        # The critical assertion: id is 1 (parsed from the encoded object),
        # NOT 0 (which is what we'd get without html.unescape).
        assert node.children[0].id == 1
        assert node.children[0].label == "Faire Kommune"

    async def test_expand_structure_intermediate_reads_structure_key(
        self, patched_keyring
    ) -> None:
        structure_html = (
            "<div class='menu-struct'>"
            "<div class='menu-struct-label' onclick='structexp(1, 7, 12, null)'>"
            "Abfall</div>"
            "</div>"
        )
        payload: dict[str, Any] = {
            "error": False,
            "options": "",
            "header": "",
            "structure": structure_html,
            "content": False,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/users/ajax_login":
                return httpx.Response(
                    200,
                    text="1",
                    headers={"Set-Cookie": "PHPSESSID=x; Path=/"},
                )
            if request.url.path == "/members/expandStructure":
                return httpx.Response(
                    200,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        expansion = await client.expand_structure(category_id=1, l1=7)
        assert expansion.subcategories is not None
        assert len(expansion.subcategories) == 1
        assert expansion.subcategories[0].label == "Abfall"

    async def test_expand_structure_leaf_returns_empty_expansion(
        self, patched_keyring
    ) -> None:
        # Leaf responses return a search form in `content`, NOT posts.
        payload: dict[str, Any] = {
            "error": False,
            "options": "",
            "header": False,
            "structure": False,
            "content": "<div class='search-form'><button>search</button></div>",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/users/ajax_login":
                return httpx.Response(
                    200,
                    text="1",
                    headers={"Set-Cookie": "PHPSESSID=x; Path=/"},
                )
            if request.url.path == "/members/expandStructure":
                return httpx.Response(
                    200,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        expansion = await client.expand_structure(category_id=1, l1=7, l2=115)
        assert expansion.subcategories is None
        assert expansion.posts is None


# ---------------------------------------------------------------------------
# C7: 401-retry path
# ---------------------------------------------------------------------------


class TestAuthRetry:
    async def test_401_triggers_relogin_then_retries(self, patched_keyring) -> None:
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if request.url.path == "/users/ajax_login":
                return httpx.Response(
                    200,
                    text="1",
                    headers={"Set-Cookie": "PHPSESSID=x; Path=/"},
                )
            if request.url.path == "/members/structure":
                # First structure call: 401 (triggers re-login).
                # Second structure call: success.
                if call_count == 1:
                    return httpx.Response(401, text="unauthorized")
                return httpx.Response(
                    200,
                    json={
                        "error": False,
                        "navigation": "",
                        "content": "",
                        "properties": "",
                    },
                )
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        # Pre-seed both the cookie jar AND the keyring cache so
        # ensure_session() is a no-op (forcing the 401 path to trigger relogin).
        assert client._client is not None
        client._client.cookies.set("PHPSESSID", "stale", domain="www.gribs.net")
        patched_keyring.append(
            {
                "name": "PHPSESSID",
                "value": "stale",
                "domain": "www.gribs.net",
                "path": "/",
            }
        )
        node = await client.list_categories(category_id=1)
        assert node is not None
        # Three calls: structure(401) + login + structure(ok).
        assert call_count == 3

    async def test_recentposts_does_not_relogin_on_401(self, patched_keyring) -> None:
        # Public endpoint — 401 should surface as GribsApiError, not trigger
        # a re-login attempt.
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if request.url.path == "/post/recentposts":
                return httpx.Response(401, text="unauthorized")
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        with pytest.raises(GribsApiError, match="HTTP 401"):
            await client.recent_posts()
        # Only one call (the recentposts); no login attempt.
        assert call_count == 1


# ---------------------------------------------------------------------------
# Malformed responses
# ---------------------------------------------------------------------------


class TestMalformedResponses:
    async def test_non_json_on_json_endpoint_raises_api_error(
        self, patched_keyring
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/users/ajax_login":
                return httpx.Response(
                    200,
                    text="1",
                    headers={"Set-Cookie": "PHPSESSID=x; Path=/"},
                )
            if request.url.path == "/members/structure":
                return httpx.Response(
                    200, text="not json at all", headers={"Content-Type": "text/html"}
                )
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        with pytest.raises(GribsApiError, match="non-JSON"):
            await client.list_categories(category_id=1)

    async def test_api_error_field_raises(self, patched_keyring) -> None:
        payload = {"error": True, "message": "boom"}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/users/ajax_login":
                return httpx.Response(
                    200,
                    text="1",
                    headers={"Set-Cookie": "PHPSESSID=x; Path=/"},
                )
            if request.url.path == "/members/structure":
                return httpx.Response(200, json=payload)
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        with pytest.raises(GribsApiError, match="API error"):
            await client.list_categories(category_id=1)

    async def test_empty_navigation_returns_empty_node(self, patched_keyring) -> None:
        payload = {"error": False, "navigation": "", "content": "", "properties": ""}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/users/ajax_login":
                return httpx.Response(
                    200,
                    text="1",
                    headers={"Set-Cookie": "PHPSESSID=x; Path=/"},
                )
            if request.url.path == "/members/structure":
                return httpx.Response(200, json=payload)
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        node = await client.list_categories(category_id=1, root_label="Antragsbörse")
        assert node.children == []
        assert node.label == "Antragsbörse"


# ---------------------------------------------------------------------------
# C3: post_id mismatch guard
# ---------------------------------------------------------------------------


class TestPostIdMismatchGuard:
    async def test_get_post_raises_on_post_id_mismatch(self, patched_keyring) -> None:
        # The response carries a different post_id than requested.
        payload = {
            "error": False,
            "content": (
                "<div><h1 class='posts-title'>Test</h1>"
                "<script>postWidget(9999, 'share');</script></div>"
            ),
            "header": "",
            "views": "",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/users/ajax_login":
                return httpx.Response(
                    200,
                    text="1",
                    headers={"Set-Cookie": "PHPSESSID=x; Path=/"},
                )
            if request.url.path == "/members/singlepost":
                return httpx.Response(200, json=payload)
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        with pytest.raises(GribsApiError, match="post_id mismatch"):
            await client.get_post(post_id=1234)

    async def test_get_post_raises_when_post_id_uninferable(
        self, patched_keyring
    ) -> None:
        payload = {
            "error": False,
            "content": "<div><h1 class='posts-title'>Test</h1></div>",
            "header": "",
            "views": "",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/users/ajax_login":
                return httpx.Response(
                    200,
                    text="1",
                    headers={"Set-Cookie": "PHPSESSID=x; Path=/"},
                )
            if request.url.path == "/members/singlepost":
                return httpx.Response(200, json=payload)
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        with pytest.raises(GribsApiError, match="Could not infer post_id"):
            await client.get_post(post_id=1234)

    async def test_get_post_succeeds_when_ids_match(self, patched_keyring) -> None:
        payload = {
            "error": False,
            "content": (
                "<div><h1 class='posts-title'>Test</h1>"
                "<span class='post-view-count'>42</span>"
                "<script>postWidget(1234, 'share');</script></div>"
            ),
            "header": "<h1 class='member-banner-title'>Umwelt</h1>",
            "views": (
                '<div onclick="inlinelink({cat:1, l1:7, l2:115, '
                'l3:null, post_id:1234})">x</div>'
            ),
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/users/ajax_login":
                return httpx.Response(
                    200,
                    text="1",
                    headers={"Set-Cookie": "PHPSESSID=x; Path=/"},
                )
            if request.url.path == "/members/singlepost":
                return httpx.Response(200, json=payload)
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        post = await client.get_post(post_id=1234)
        assert post.post_id == 1234
        assert post.view_count == 42
        assert "Umwelt" in post.category_breadcrumb


# ---------------------------------------------------------------------------
# I5: stale cookies trigger login
# ---------------------------------------------------------------------------


class TestStaleCookiesTriggerLogin:
    async def test_ensure_session_logs_in_when_no_cached_cookies(
        self, patched_keyring
    ) -> None:
        login_called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal login_called
            if request.url.path == "/users/ajax_login":
                login_called = True
                return httpx.Response(
                    200,
                    text="1",
                    headers={"Set-Cookie": "PHPSESSID=x; Path=/"},
                )
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        await client.ensure_session()
        assert login_called


# ---------------------------------------------------------------------------
# resolve_post_id (Limitation 2)
# ---------------------------------------------------------------------------


class TestResolvePostId:
    async def test_resolves_wp_id_to_post_id(self, patched_keyring) -> None:
        # Live-verified: GET /?wp=<id> redirects to /members/home/wp-<id>
        # which contains `"post_id":"<N>"` in a JS blob.
        member_html = (
            '<html><script>window.__DATA__ = {"post_id":"3097"};</script></html>'
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/" and b"wp=19880" in request.url.query:
                # Simulate the redirect landing directly (MockTransport doesn't
                # follow redirects unless we return a 3xx + Location).
                return httpx.Response(
                    200,
                    text=member_html,
                    headers={"Content-Type": "text/html"},
                )
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        ref = await client.resolve_post_id(wp_id=19880)
        assert ref.wp_id == 19880
        assert ref.post_id == 3097
        assert ref.url == "https://www.gribs.net/?wp=19880"
        assert ref.retrieved_at is not None

    async def test_raises_when_post_id_not_found(self, patched_keyring) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/":
                return httpx.Response(
                    200,
                    text="<html><body>no post_id here</body></html>",
                    headers={"Content-Type": "text/html"},
                )
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        with pytest.raises(GribsApiError, match="could not extract post_id"):
            await client.resolve_post_id(wp_id=99999)

    async def test_raises_on_http_error(self, patched_keyring) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="server error")

        client = _make_client(handler)
        with pytest.raises(GribsApiError, match="HTTP 500"):
            await client.resolve_post_id(wp_id=19880)


# ---------------------------------------------------------------------------
# Concurrent recent_posts enrichment (Limitation 4)
# ---------------------------------------------------------------------------


class TestConcurrentRecentPosts:
    async def test_enrichment_is_concurrent(self, patched_keyring) -> None:
        # 3 scaffolds -> 3 concurrent fill_post_widget calls. We observe
        # concurrency by recording call start/end times: if enrichment were
        # sequential, total time would be ~3x the per-call delay. With gather,
        # all 3 fill calls are in-flight simultaneously.
        import asyncio as _asyncio
        import time

        recent_html = (
            "<div class='startpage-recent'>"
            "<div class='post-widget' id='601_pid'>"
            "<script>postWidget(601,'601_pid','start');</script></div>"
            "<div class='post-widget' id='602_pid'>"
            "<script>postWidget(602,'602_pid','start');</script></div>"
            "<div class='post-widget' id='603_pid'>"
            "<script>postWidget(603,'603_pid','start');</script></div>"
            "</div>"
        )

        fill_call_count = 0
        fill_call_starts: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal fill_call_count
            if request.url.path == "/post/recentposts":
                return httpx.Response(
                    200, text=recent_html, headers={"Content-Type": "text/html"}
                )
            if request.url.path == "/members/postWidgetFill":
                fill_call_count += 1
                fill_call_starts.append(time.monotonic())
                widget_html = (
                    f"<div class='pwidget-title' title='Post {fill_call_count}'>"
                    f"Post {fill_call_count}</div>"
                )
                return httpx.Response(
                    200, text=widget_html, headers={"Content-Type": "text/html"}
                )
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        # Monkey-patch fill_post_widget to add a small delay so concurrency
        # is observable via timing. The gather pattern means all 3 calls
        # start before any finish.
        original_fill = client.fill_post_widget

        async def _slow_fill(post_id: int, caller: str = "start") -> str:
            await _asyncio.sleep(0.05)
            return await original_fill(post_id, caller)

        client.fill_post_widget = _slow_fill  # type: ignore[method-assign]

        start = time.monotonic()
        posts = await client.recent_posts()
        elapsed = time.monotonic() - start

        assert len(posts) == 3
        assert fill_call_count == 3
        # If sequential: 3 * 0.05 = 0.15s minimum. With gather: ~0.05s.
        # Allow generous slack for CI; the key assertion is that it's
        # significantly faster than sequential.
        assert elapsed < 0.12, (
            f"enrichment appears sequential (elapsed={elapsed:.3f}s, "
            "expected <0.12s for concurrent 3x0.05s)"
        )
        # All 3 fill calls started within a small window (concurrency check):
        # the difference between the first and last start should be tiny.
        if len(fill_call_starts) >= 2:
            spread = max(fill_call_starts) - min(fill_call_starts)
            assert spread < 0.02, (
                f"fill calls started too far apart (spread={spread:.3f}s, "
                "expected <0.02s for concurrent dispatch)"
            )

    async def test_preserves_scaffold_order(self, patched_keyring) -> None:
        # asyncio.gather preserves ordering: results come back in the same
        # order as the input scaffolds, regardless of completion order.
        recent_html = (
            "<div class='startpage-recent'>"
            "<div class='post-widget' id='701_pid'>"
            "<script>postWidget(701,'701_pid','start');</script></div>"
            "<div class='post-widget' id='702_pid'>"
            "<script>postWidget(702,'702_pid','start');</script></div>"
            "<div class='post-widget' id='703_pid'>"
            "<script>postWidget(703,'703_pid','start');</script></div>"
            "</div>"
        )

        # Make the middle post's fill respond slowest to verify ordering
        # is by scaffold position, not completion time.
        import asyncio as _asyncio

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/post/recentposts":
                return httpx.Response(
                    200, text=recent_html, headers={"Content-Type": "text/html"}
                )
            if request.url.path == "/members/postWidgetFill":
                post_id = request.read().decode().split("post_id=")[1].split("&")[0]
                # Middle post is slowest.
                widget_html = (
                    f"<div class='pwidget-title' title='Post {post_id}'>"
                    f"Post {post_id}</div>"
                )
                return httpx.Response(
                    200,
                    text=widget_html,
                    headers={"Content-Type": "text/html"},
                )
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        original_fill = client.fill_post_widget

        async def _variable_fill(post_id: int, caller: str = "start") -> str:
            if post_id == 702:
                await _asyncio.sleep(0.03)  # middle is slowest
            return await original_fill(post_id, caller)

        client.fill_post_widget = _variable_fill  # type: ignore[method-assign]

        posts = await client.recent_posts()
        assert [p.post_id for p in posts] == [701, 702, 703]
        assert [p.title for p in posts] == ["Post 701", "Post 702", "Post 703"]


# ---------------------------------------------------------------------------
# Logging (caplog tests — verify sensitive data isn't logged)
# ---------------------------------------------------------------------------


class TestLogging:
    """Verify logging calls fire at the right levels without leaking secrets."""

    async def test_login_success_logs_email_not_password(
        self, patched_keyring, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging as _logging

        handler = _set_cookie_handler()
        client = _make_client(handler)
        with caplog.at_level(_logging.DEBUG, logger="gribs_mcp.client"):
            await client.login()
        # The success log should mention the email.
        assert any("tester@example.org" in r.message for r in caplog.records)
        # The password must NEVER appear in any log record.
        assert "hunter2" not in caplog.text

    async def test_401_retry_logs_relogin(
        self, patched_keyring, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging as _logging

        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if request.url.path == "/users/ajax_login":
                return httpx.Response(
                    200,
                    text="1",
                    headers={"Set-Cookie": "PHPSESSID=x; Path=/"},
                )
            if request.url.path == "/members/structure":
                if call_count == 1:
                    return httpx.Response(401, text="unauthorized")
                return httpx.Response(
                    200,
                    json={
                        "error": False,
                        "navigation": "",
                        "content": "",
                        "properties": "",
                    },
                )
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        assert client._client is not None
        client._client.cookies.set("PHPSESSID", "stale", domain="www.gribs.net")
        patched_keyring.append(
            {
                "name": "PHPSESSID",
                "value": "stale",
                "domain": "www.gribs.net",
                "path": "/",
            }
        )
        with caplog.at_level(_logging.INFO, logger="gribs_mcp.client"):
            await client.list_categories(category_id=1)
        # The 401 retry should log an INFO message about re-logging in.
        assert any("re-logging in" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Issue #3: concurrent 401s collapse to a single login
# ---------------------------------------------------------------------------


class TestConcurrentRelogin:
    """N concurrent 401s must produce exactly 1 login call.

    The fix re-checks the cookie cache after acquiring `_login_lock`: the
    first waiter logs in and persists cookies via `auth.store_cookies`; all
    subsequent waiters see fresh cookies and skip `login()`. This test fires
    N=5 concurrent authenticated requests whose first attempt 401s, and
    asserts the login endpoint was hit exactly once.
    """

    async def test_n_concurrent_401s_trigger_one_login(self, patched_keyring) -> None:
        import asyncio

        N = 5
        login_calls = 0
        search_calls = 0
        # Gate the first login attempt so all N search coroutines have already
        # 401'd and queued on `_login_lock` before the login completes. This
        # makes the concurrency observable rather than a trivial sequential
        # pass where the first search finishes its relogin before others
        # start.
        all_searches_401d = asyncio.Event()

        search_html = (
            "<div class='search-results'>"
            "<div class='search-result-div'>"
            "<div class='headline' onclick=\"document.location.href="
            "'https://www.gribs.net/?wp=1'\">Hit</div>"
            "<div class='bodyline'>body</div>"
            "</div>"
            "</div>"
        )

        def _session_cookie_value(request: httpx.Request) -> str | None:
            # `httpx.Request` has no `.cookies` attribute; the jar is serialized
            # into the `Cookie` header on the outgoing request.
            raw = request.headers.get("Cookie") or request.headers.get("cookie")
            if not raw:
                return None
            for part in raw.split(";"):
                part = part.strip()
                if part.startswith("PHPSESSID="):
                    return part[len("PHPSESSID=") :]
            return None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal login_calls, search_calls
            if request.url.path == "/users/ajax_login":
                login_calls += 1
                return httpx.Response(
                    200,
                    text="1",
                    headers={"Set-Cookie": "PHPSESSID=x; Path=/"},
                )
            if request.url.path == "/members/search":
                search_calls += 1
                # Reject the stale pre-seeded cookie (any value != "x"); once
                # relogin succeeds the jar carries PHPSESSID=x and the retry
                # returns 200.
                if _session_cookie_value(request) != "x":
                    if search_calls == N:
                        all_searches_401d.set()
                    return httpx.Response(401, text="unauthorized")
                return httpx.Response(
                    200, text=search_html, headers={"Content-Type": "text/html"}
                )
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        # Pre-seed a stale cookie in the jar so the first POST to /members/search
        # is authenticated-but-rejected (401). We DON'T pre-seed the keyring
        # cache — `ensure_session` is patched to a no-op below so it never tries
        # to log in outside the `_request_with_retry` lock (which would defeat
        # the test's single-login assertion).
        assert client._client is not None
        client._client.cookies.set("PHPSESSID", "stale", domain="www.gribs.net")

        # Patch `ensure_session` to a no-op: without this, once the first waiter
        # deletes the keyring cache (inside the lock), subsequent waiters'
        # `ensure_session()` would call `login()` OUTSIDE the lock — a second
        # login path the issue's fix doesn't cover. Isolating the test to the
        # `_request_with_retry` 401 path is the intended scope.
        async def _noop_ensure_session() -> None:
            return None

        client.ensure_session = _noop_ensure_session  # type: ignore[method-assign]

        # Wrap login() to gate the first invocation on all_searches_401d so all
        # N waiters queue on `_login_lock` before the first login completes.
        original_login = client.login

        async def _gated_login(*args: Any, **kwargs: Any) -> None:
            await all_searches_401d.wait()
            return await original_login(*args, **kwargs)

        client.login = _gated_login  # type: ignore[method-assign]

        results = await asyncio.gather(
            *(client.search(query="test", category_id=1) for _ in range(N))
        )

        assert all(len(r) == 1 for r in results), (
            f"each concurrent search should return 1 hit; got {results!r}"
        )
        assert login_calls == 1, (
            f"expected exactly 1 login for {N} concurrent 401s; got {login_calls}"
        )


# ---------------------------------------------------------------------------
# Issue #4: enrichment failure surfaces a warning log
# ---------------------------------------------------------------------------


class TestEnrichmentFailureLogsWarning:
    """A failed `postWidgetFill` for one post must keep the scaffold AND log.

    The caller (an MCP tool) has no way to distinguish "post genuinely has no
    title" from "we lost auth mid-fetch" — at minimum, the degradation must
    be visible in logs.
    """

    async def test_enrichment_failure_logs_warning(
        self, patched_keyring, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        recent_html = (
            "<div class='startpage-recent'>"
            "<div class='post-widget' id='801_pid'>"
            "<script>postWidget(801,'801_pid','start');</script></div>"
            "<div class='post-widget' id='802_pid'>"
            "<script>postWidget(802,'802_pid','start');</script></div>"
            "<div class='post-widget' id='803_pid'>"
            "<script>postWidget(803,'803_pid','start');</script></div>"
            "</div>"
        )
        ok_widget = (
            "<div class='pwidget-title' title='Enriched {pid}'>Enriched {pid}</div>"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/post/recentposts":
                return httpx.Response(
                    200, text=recent_html, headers={"Content-Type": "text/html"}
                )
            if request.url.path == "/members/postWidgetFill":
                body = request.read().decode()
                # post_id=802 returns 500 -> GribsApiError in the client.
                if "post_id=802" in body:
                    return httpx.Response(500, text="server error")
                pid = "801" if "post_id=801" in body else "803"
                return httpx.Response(
                    200,
                    text=ok_widget.format(pid=pid),
                    headers={"Content-Type": "text/html"},
                )
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        with caplog.at_level(logging.WARNING, logger="gribs_mcp.client"):
            posts = await client.recent_posts()

        # All 3 scaffolds retained — no post dropped.
        assert len(posts) == 3
        assert [p.post_id for p in posts] == [801, 802, 803]
        # The two successful enrichments carry their parsed titles.
        assert posts[0].title == "Enriched 801"
        assert posts[2].title == "Enriched 803"
        # The failed one fell back to the scaffold title ("Post <id>").
        assert posts[1].title == "Post 802"
        # The failure was surfaced as a WARNING log.
        assert any(
            "Enrichment failed for post_id=802" in r.message
            and r.levelno == logging.WARNING
            for r in caplog.records
        ), f"expected WARNING about post_id=802; got {caplog.records!r}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
