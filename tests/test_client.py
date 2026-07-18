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

import json
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


# Silence unused-import for `json` (used inline above for clarity in fixtures).
_ = json


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
