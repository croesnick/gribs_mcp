"""HTTP client for gribs.net — auth, cookie handling, API methods.

Per INTENT.md §"API-Endpoints" and the live-API verification (2026-07-18):
- All calls are POST form-urlencoded with header `X-Requested-With: XMLHttpRequest`.
- Two response shapes coexist:
    * JSON `{error, ...}` for `/members/structure`, `/members/singlepost`,
      `/members/expandStructure` (key carrying the payload varies per endpoint).
    * Raw HTML for `/members/search` and `/post/recentposts` (no JSON wrapper).
- Login (`/users/ajax_login`) returns a 15-byte non-JSON status; success is
  signalled by Set-Cookie.

The client uses httpx.AsyncClient with a cookie jar. On 401/403 or expired
cookies, it re-logs in automatically via `auth.py` (guarded by an asyncio.Lock
to avoid concurrent-login races).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from gribs_mcp import __version__, auth, parsers
from gribs_mcp.auth import AuthError, CookieEntry
from gribs_mcp.models import (
    CategoryNode,
    PostDetail,
    PostIdRef,
    PostTeaser,
    SearchHit,
    StructureExpansion,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.gribs.net"
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
USER_AGENT = f"gribs-mcp/{__version__} (+https://github.com/croesnick/gribs_mcp)"


def _utcnow() -> datetime:
    """Return current UTC time (tz-aware)."""
    return datetime.now(UTC)


def _serialize_cookies(jar: httpx.Cookies) -> list[CookieEntry]:
    """Serialize an httpx cookie jar into the keyring-cache format (pure)."""
    entries: list[CookieEntry] = []
    for cookie in jar.jar:
        entries.append(
            {
                "name": cookie.name,
                "value": cookie.value or "",
                "domain": cookie.domain or "www.gribs.net",
                "path": cookie.path or "/",
                "expires": cookie.expires,
            }
        )
    return entries


def _looks_like_success(body: Any) -> bool:
    """Heuristic: a login response indicating success.

    gribs returns ~15-byte non-JSON status payloads. Treat truthy non-dict
    values as success. The dict case is handled separately in
    :meth:`GribsClient._validate_login_response` (this helper is only reached
    when no session cookie was set, to decide whether to surface a failure).
    """
    if body is None:
        return False
    if isinstance(body, bool):
        return body
    if isinstance(body, (int, float)):
        return body > 0
    if isinstance(body, str):
        return body.strip() in {"1", "true", "ok", "OK", "success"}
    return False


def _truncate(s: Any, limit: int = 200) -> str:
    """Truncate a value's repr to `limit` chars for safe logging/errors."""
    text = repr(s) if not isinstance(s, str) else s
    if len(text) > limit:
        return text[:limit] + "...(truncated)"
    return text


class GribsAuthError(AuthError):
    """Raised when login fails or credentials are missing.

    Subclasses `auth.AuthError` so callers that catch the auth-layer error
    also catch this client-layer variant (and vice-versa).
    """


class GribsApiError(RuntimeError):
    """Raised when the gribs API returns an error or unexpected response."""


class GribsClient:
    """Async HTTP client for gribs.net.

    One instance is meant to be reused across MCP tool calls (module-level
    singleton via :func:`get_client`). Cookie jar is backed by httpx's
    `AsyncClient.cookies` and persisted to the OS keyring on successful login.
    """

    def __init__(self, base_url: str = BASE_URL) -> None:
        self._base_url = base_url
        self._client: httpx.AsyncClient | None = None
        self._credentials: auth.Credentials | None = None
        # Serializes re-login attempts so concurrent 401s don't trigger N logins.
        self._login_lock = asyncio.Lock()
        # Bumped each time a successful re-login completes inside
        # `_request_with_retry`. Concurrent waiters capture the value before
        # acquiring the lock; if it changed by the time they get the lock,
        # another waiter already re-logged in and they can skip `login()`.
        self._relogin_generation = 0

    async def __aenter__(self) -> GribsClient:
        await self._ensure_client()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=DEFAULT_TIMEOUT,
                headers={
                    "User-Agent": USER_AGENT,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            self._client = client
            # Hydrate jar from keyring cache.
            cached = auth.load_cookies()
            if cached:
                for entry in cached:
                    name = entry.get("name")
                    value = entry.get("value")
                    if not name or not value:
                        continue
                    client.cookies.set(
                        name=name,
                        value=value,
                        domain=entry.get("domain") or "www.gribs.net",
                        path=entry.get("path") or "/",
                    )
                logger.debug("Hydrated %d cookies from keyring cache", len(cached))
            else:
                logger.debug(
                    "No cached cookies; will log in on first authenticated call"
                )
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def login(
        self, email: str | None = None, password: str | None = None
    ) -> None:
        """Perform a fresh form-POST login and persist cookies to keyring.

        Args:
            email: Email; if None, loads from keyring/env.
            password: Password; if None, loads from keyring/env.

        Raises:
            GribsAuthError: if credentials are missing or login fails.
        """
        if email is None or password is None:
            try:
                creds = self._credentials or auth.load_credentials()
            except AuthError as exc:
                # Wrap in GribsAuthError so callers catching the client-layer
                # error see a consistent type.
                raise GribsAuthError(str(exc)) from exc
            self._credentials = creds
            email = email or creds.email
            password = password or creds.password

        client = await self._ensure_client()
        # Don't carry stale cookies into the login.
        client.cookies.clear()

        resp = await client.post(
            "/users/ajax_login",
            data={"email": email, "password": password, "keep": "true"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            logger.warning("Login HTTP %d for email=%s", resp.status_code, email)
            raise GribsAuthError(f"Login HTTP {resp.status_code}: {resp.text[:200]}")

        # Login returns a 15-byte non-JSON status (e.g. "1" or ""). Success is
        # signalled by Set-Cookie presence. JSON parsing is best-effort only.
        body: Any = None
        with contextlib.suppress(ValueError, httpx.DecodingError):
            body = resp.json()

        self._validate_login_response(resp, body, email)

        # Persist cookies to keyring.
        entries = _serialize_cookies(client.cookies)
        auth.store_cookies(entries)
        self._credentials = auth.Credentials(email=email, password=password)
        logger.debug(
            "Login successful for email=%s; cached %d cookies",
            email,
            len(entries),
        )

    @staticmethod
    def _validate_login_response(resp: httpx.Response, body: Any, email: str) -> None:
        """Validate the login response, raising GribsAuthError on rejection.

        Args:
            resp: The httpx response (used for cookie inspection — never logged).
            body: Best-effort parsed JSON body (may be None if non-JSON).
            email: The email used (for logging — never the password).

        Raises:
            GribsAuthError: if the login was rejected (error field set) or
                no session cookie was set and the body doesn't look like success.
        """
        cookie_set = bool(resp.cookies)
        if isinstance(body, dict):
            err = body.get("error")
            if err not in (None, False, "false", 0, "0"):
                logger.warning(
                    "Login rejected for email=%s: body=%s", email, _truncate(body)
                )
                raise GribsAuthError(f"Login rejected: body={_truncate(body)}")

        if not cookie_set and not _looks_like_success(body):
            logger.warning(
                "Login did not set session cookie for email=%s (body=%s)",
                email,
                _truncate(body),
            )
            raise GribsAuthError(
                f"Login did not set a session cookie (body={_truncate(body)})"
            )

    async def ensure_session(self) -> None:
        """Ensure we have a fresh logged-in session.

        If cookies are cached and fresh, this is a no-op. Otherwise it logs in.
        """
        cached = auth.load_cookies()
        if cached:
            # Cached cookies exist and are fresh — ensure they're in the jar.
            await self._ensure_client()
            return
        await self.login()

    # ------------------------------------------------------------------
    # Low-level request helpers
    # ------------------------------------------------------------------

    async def _request_with_retry(
        self,
        endpoint: str,
        data: dict[str, Any],
        *,
        allow_relogin: bool = True,
    ) -> httpx.Response:
        """POST form-urlencoded and retry once after re-login on 401/403.

        Args:
            endpoint: Path under base_url.
            data: Form fields.
            allow_relogin: If False, skip the 401/403 re-login path (used by
                public endpoints like `/post/recentposts`).

        Raises:
            GribsAuthError: on 401/403 when re-login fails or is disallowed.
            GribsApiError: on other HTTP errors.
        """
        client = await self._ensure_client()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        resp = await client.post(endpoint, data=data, headers=headers)

        if resp.status_code in (401, 403) and allow_relogin:
            logger.info("Got HTTP %d on %s; re-logging in", resp.status_code, endpoint)
            # Serialize the re-login decision so concurrent 401s collapse to a
            # single login. Capture the relogin generation BEFORE acquiring
            # the lock; if it changed by the time we get the lock, another
            # waiter already re-logged in and we can skip `login()` entirely.
            # N concurrent 401s therefore result in 1 login, not N.
            my_generation = self._relogin_generation
            async with self._login_lock:
                if self._relogin_generation == my_generation:
                    # No waiter has re-logged in since we got our 401 — we're
                    # the one that logs in. Clear the rejected cookies from
                    # both the keyring cache and the live jar.
                    # `load_cookies()` freshness checks (age/expiry) don't
                    # catch server-side revocation, so we force-clear here.
                    auth.delete_cookies()
                    client.cookies.clear()
                    try:
                        await self.login()
                    except GribsAuthError as exc:
                        raise GribsAuthError(f"Re-login failed: {exc}") from exc
                    self._relogin_generation += 1
                else:
                    # Another waiter already re-logged in; hydrate our jar
                    # from the fresh cache so the retry POST carries valid
                    # cookies.
                    cached = auth.load_cookies() or []
                    client.cookies.clear()
                    for entry in cached:
                        name = entry.get("name")
                        value = entry.get("value")
                        if not name or not value:
                            continue
                        client.cookies.set(
                            name=name,
                            value=value,
                            domain=entry.get("domain") or "www.gribs.net",
                            path=entry.get("path") or "/",
                        )
                    logger.debug(
                        "Re-login already performed by another waiter; "
                        "hydrated %d cookies for retry",
                        len(cached),
                    )
            # Retry POST outside the lock so concurrent retries don't
            # serialize.
            resp = await client.post(endpoint, data=data, headers=headers)

        if resp.status_code >= 400:
            raise GribsApiError(
                f"POST {endpoint} -> HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return resp

    async def _post_form_json(
        self,
        endpoint: str,
        data: dict[str, Any],
        *,
        allow_relogin: bool = True,
    ) -> dict[str, Any]:
        """POST and return parsed JSON dict, with auth retry.

        Raises:
            GribsAuthError: on 401/403 after a re-login attempt.
            GribsApiError: on other HTTP errors or malformed/non-dict JSON.
        """
        resp = await self._request_with_retry(
            endpoint, data, allow_relogin=allow_relogin
        )
        try:
            payload = resp.json()
        except (ValueError, httpx.DecodingError) as exc:
            raise GribsApiError(f"POST {endpoint} -> non-JSON response: {exc}") from exc

        if not isinstance(payload, dict):
            logger.warning("POST %s returned non-dict JSON: %r", endpoint, payload)
            raise GribsApiError(
                f"POST {endpoint} -> unexpected JSON shape: {payload!r}"
            )

        if payload.get("error") not in (None, False, "false", 0, "0"):
            raise GribsApiError(f"POST {endpoint} -> API error: {payload}")

        return payload

    async def _post_form_html(
        self,
        endpoint: str,
        data: dict[str, Any],
        *,
        allow_relogin: bool = True,
    ) -> str:
        """POST and return raw HTML response text (no JSON wrapper).

        Used for `/members/search` and `/post/recentposts` which return plain
        HTML regardless of the `X-Requested-With: XMLHttpRequest` header.

        Raises:
            GribsAuthError: on 401/403 after a re-login attempt (when allowed).
            GribsApiError: on other HTTP errors.
        """
        resp = await self._request_with_retry(
            endpoint, data, allow_relogin=allow_relogin
        )
        return resp.text

    # ------------------------------------------------------------------
    # API methods
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        category_id: int = 1,
        whole_word: bool = False,
        l1: int | None = None,
        l2: int | None = None,
        l3: int | None = None,
    ) -> list[SearchHit]:
        """Search posts via `/members/search`.

        The endpoint returns raw HTML (NOT JSON), Content-Type `text/html`.
        Scoped search via `conditions[category_level1_id]` / `[l2]` / `[l3]`.

        Args:
            query: Search string.
            category_id: Top-level category (1 = Antragsbörse).
            whole_word: When True, request whole-word matches.
            l1: Optional L1 sub-category id (scoped search).
            l2: Optional L2 sub-category id (scoped search).
            l3: Optional L3 sub-category id (scoped search).

        Returns:
            Up to 50 SearchHit objects.
        """
        await self.ensure_session()
        data: dict[str, str] = {
            "searchstring": query,
            "checkval": "true" if whole_word else "false",
            "conditions[deleted]": "0",
            "conditions[status]": "10",
            "conditions[category_id]": str(category_id),
        }
        if l1 is not None:
            data["conditions[category_level1_id]"] = str(l1)
        if l2 is not None:
            data["conditions[category_level2_id]"] = str(l2)
        if l3 is not None:
            data["conditions[category_level3_id]"] = str(l3)
        html = await self._post_form_html("/members/search", data)
        return await asyncio.to_thread(parsers.parse_search_results, html)

    async def get_post(self, post_id: int) -> PostDetail:
        """Fetch full post detail via `/members/singlepost`.

        Args:
            post_id: Internal gribs post id (NOT wp_id).

        Returns:
            PostDetail with body, metadata, breadcrumb, and source URL.

        Raises:
            GribsApiError: if the parsed post_id doesn't match the requested
                `post_id` (silent-wrong-post guard, see Oracle C3).
        """
        await self.ensure_session()
        payload = await self._post_form_json(
            "/members/singlepost", {"post_id": str(post_id)}
        )
        post = await parsers.parse_singlepost_async(payload)
        # Guard against silent wrong-post fetches: if we couldn't confirm the
        # post_id, or it disagrees with what was requested, surface it loudly.
        if post.post_id is None:
            raise GribsApiError(
                f"Could not infer post_id from singlepost response for "
                f"requested post_id={post_id}"
            )
        if post.post_id != post_id:
            raise GribsApiError(
                f"post_id mismatch: requested {post_id}, got {post.post_id}"
            )
        return post

    async def list_categories(
        self, category_id: int = 1, root_label: str = ""
    ) -> CategoryNode:
        """Fetch the L1 category tree via `/members/structure`.

        The response JSON has keys `{error, navigation, content, properties}`.
        The category tree lives in `navigation` (NOT `content`, which holds
        landing-page widgets).

        Args:
            category_id: Top-level category (1 = Antragsbörse).
            root_label: Human-readable label for the synthesized root node
                (e.g. "Antragsbörse"). The response HTML does not echo the
                section name back.

        Returns:
            CategoryNode with L1 children.
        """
        await self.ensure_session()
        data = {
            "obj[cat]": str(category_id),
            "idsuff": str(category_id),
            "type": "load",
            "sort": "false",
        }
        payload = await self._post_form_json("/members/structure", data)
        navigation = payload.get("navigation", "")
        if not isinstance(navigation, str):
            return CategoryNode(id=category_id, label=root_label, children=[])
        return await asyncio.to_thread(parsers.parse_structure, navigation, root_label)

    async def expand_structure(
        self,
        category_id: int,
        l1: int | None = None,
        l2: int | None = None,
        l3: int | None = None,
    ) -> StructureExpansion:
        """Expand a structure node via `/members/expandStructure`.

        Response JSON has keys `{error, options, header, structure, content}`:
        - Intermediate nodes: `structure` contains L2+ subcategory HTML,
          `content` is `false`.
        - Leaf nodes: `content` contains a **search form** (NOT post listings),
          `structure` is `false`. Posts must be fetched via :meth:`search` with
          `conditions[category_level*_id]` params.

        Args:
            category_id: Top-level category id.
            l1: First-level id (or None for L1 root).
            l2: Second-level id.
            l3: Third-level id.

        Returns:
            StructureExpansion with `subcategories` (intermediate) populated,
            or both fields `None` for a leaf (caller should use :meth:`search`).
        """
        await self.ensure_session()
        # Request body per INTENT.md §API-Endpoints:
        #   obj[cat]=1&idsuff=1&type=load&sort=false          (load L1 root)
        #   obj[cat]=1&obj[l1]=7&idsuff=7&type=expand&sort=…  (expand sub-node)
        # `idsuff` is the deepest level id provided (or the cat_id at L1 root).
        data: dict[str, str] = {"obj[cat]": str(category_id), "sort": "false"}
        if l1 is None and l2 is None and l3 is None:
            data["idsuff"] = str(category_id)
            data["type"] = "load"
        else:
            data["type"] = "expand"
            deepest = l3 if l3 is not None else (l2 if l2 is not None else l1)
            if deepest is not None:
                data["idsuff"] = str(deepest)
            if l1 is not None:
                data["obj[l1]"] = str(l1)
            if l2 is not None:
                data["obj[l2]"] = str(l2)
            if l3 is not None:
                data["obj[l3]"] = str(l3)
        payload = await self._post_form_json("/members/expandStructure", data)

        # Intermediate nodes carry subcategories in `structure`.
        structure = payload.get("structure", "")
        if isinstance(structure, str) and structure:
            return await asyncio.to_thread(parsers.parse_expand_structure, structure)

        # Leaf nodes return a search form in `content` (NOT posts). The caller
        # must use search() with the category_level*_id conditions to list
        # posts in a leaf — there is no direct listing here.
        return StructureExpansion(subcategories=None, posts=None)

    async def resolve_post_id(self, wp_id: int) -> PostIdRef:
        """Resolve a WordPress `wp_id` to an internal `post_id` via `GET /?wp=<id>`.

        Live-verified path: `GET /?wp=<id>` with `follow_redirects=True` returns
        the member page at `/members/home/wp-<id>` which contains
        `"post_id":"<N>"` (or `"post_id":<N>`) in a JSON-ish context. We
        regex-extract the first match.

        This is the bridge from `search_antraege` (which returns `wp_id`s) to
        `get_antrag` (which needs `post_id`s).

        Args:
            wp_id: WordPress post id (from a `?wp=<id>` deep link).

        Returns:
            PostIdRef with `wp_id`, `post_id`, `url`, and `retrieved_at`
            (Quellenpflicht).

        Raises:
            GribsApiError: if the request fails or no `post_id` can be
                extracted from the response.
        """
        client = await self._ensure_client()
        try:
            resp = await client.get(f"/?wp={wp_id}", follow_redirects=True)
        except httpx.HTTPError as exc:
            raise GribsApiError(f"GET /?wp={wp_id} -> transport error: {exc}") from exc
        if resp.status_code >= 400:
            raise GribsApiError(
                f"GET /?wp={wp_id} -> HTTP {resp.status_code}: {resp.text[:200]}"
            )
        post_id = parsers.parse_post_id_from_member_page(resp.text)
        if post_id is None:
            logger.warning(
                "Could not extract post_id from /?wp=%d response (len=%d)",
                wp_id,
                len(resp.text),
            )
            raise GribsApiError(
                f"GET /?wp={wp_id} -> could not extract post_id from response"
            )
        return PostIdRef(
            wp_id=wp_id,
            post_id=post_id,
            url=f"https://www.gribs.net/?wp={wp_id}",
            retrieved_at=_utcnow(),
        )

    async def fill_post_widget(self, post_id: int, caller: str = "start") -> str:
        """Fetch the teaser HTML for a single post via `/members/postWidgetFill`.

        The browser fires this XHR after `postWidget(N, pid, caller)` to lazy-
        load the actual teaser content (title, date, image). Returns raw HTML
        (Content-Type `text/html`, NOT JSON).

        Args:
            post_id: Internal gribs post id.
            caller: Caller context tag (e.g. 'start', 'members'). Defaults to
                'start' (the recentposts context).

        Returns:
            Raw HTML snippet from the response body.
        """
        return await self._post_form_html(
            "/members/postWidgetFill",
            {"post_id": str(post_id), "caller": caller},
            allow_relogin=False,
        )

    async def recent_posts(self) -> list[PostTeaser]:
        """Fetch the newest 3 posts via `/post/recentposts`.

        Public endpoint — no authentication, no 401 relogin path.
        Returns raw HTML (Content-Type `text/html`, NOT JSON).

        The recentposts response contains only post widget scaffolds (post_id
        + spinner), NOT titles. The browser lazy-loads each teaser via
        `/members/postWidgetFill`. This method performs that N+1 enrichment
        concurrently: 1 request for the scaffold + N parallel `postWidgetFill`
        calls (3 parallel for the default 3 posts). The order of results
        matches the scaffold order (asyncio.gather preserves ordering).

        Returns:
            Up to 3 PostTeaser objects with title (full, from `title` attr),
            date, and members-landing URL fallback. `snippet` is None
            (postWidgetFill doesn't return a snippet).
        """
        scaffold_html = await self._post_form_html(
            "/post/recentposts", {}, allow_relogin=False
        )
        scaffolds = await asyncio.to_thread(parsers.parse_recent_posts, scaffold_html)
        if not scaffolds:
            return []

        async def _enrich(scaffold: PostTeaser) -> PostTeaser:
            try:
                widget_html = await self.fill_post_widget(scaffold.post_id)
                return await asyncio.to_thread(
                    parsers.parse_post_widget, widget_html, scaffold.post_id
                )
            except (GribsApiError, httpx.HTTPError) as exc:
                # If enrichment fails for one post, keep the scaffold (title
                # will be "Post <id>") rather than dropping it entirely. Surface
                # the failure in logs so the degradation isn't silent — a
                # caller seeing placeholder titles can correlate via this
                # warning.
                logger.warning(
                    "Enrichment failed for post_id=%d: %s", scaffold.post_id, exc
                )
                return scaffold

        # Concurrent enrichment — all fill_post_widget calls fire in parallel.
        return list(await asyncio.gather(*(_enrich(s) for s in scaffolds)))


# Module-level singleton getter (per INTENT.md: do not instantiate per-call).
_CLIENT: GribsClient | None = None


def get_client() -> GribsClient:
    """Return the module-level GribsClient singleton."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = GribsClient()
    return _CLIENT


__all__ = [
    "BASE_URL",
    "GribsApiError",
    "GribsAuthError",
    "GribsClient",
    "get_client",
]
