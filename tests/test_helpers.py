"""Direct unit tests for extracted helpers (Issue #9).

These helpers previously had only indirect coverage via the integration tests
in :mod:`tests.test_client` (mocked httpx transport). This module tests them
directly with edge cases that are awkward to exercise through the full client
flow — empty/missing cookie fields, boundary inputs to ``_clamp``, unknown
category names, etc.

These tests do NOT touch the OS keyring and do NOT instantiate ``GribsClient``
(we call the staticmethod ``_validate_login_response`` directly). Keep them
pure-function style: no ``patched_keyring`` / ``fake_credentials`` fixtures.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

# `GribsApiError` is defined in client.py and re-imported by server.py; both
# modules share the same class object, so importing it once from client is
# sufficient regardless of which helper raised it.
from gribs_mcp.client import (
    GribsApiError,
    GribsAuthError,
    GribsClient,
    _looks_like_success,
    _serialize_cookies,
    _truncate,
)
from gribs_mcp.server import (
    CATEGORY_IDS,
    _clamp,
    _format_known_categories,
    _resolve_category_id,
)

_EMAIL = "user@example.org"
_LOGIN_URL = "https://www.gribs.net/users/ajax_login"


def _make_login_response(*, with_cookie: bool = False) -> httpx.Response:
    """Build an httpx.Response mimicking a login reply.

    httpx requires a request instance attached to the response before it will
    parse ``Set-Cookie`` headers, so we attach a synthetic POST request.
    """
    req = httpx.Request("POST", _LOGIN_URL)
    headers: list[tuple[str, str]] = (
        [("Set-Cookie", "session=abc; Path=/; Domain=gribs.net")] if with_cookie else []
    )
    return httpx.Response(200, request=req, headers=headers)


# ---------------------------------------------------------------------------
# _validate_login_response (GribsClient staticmethod)
# ---------------------------------------------------------------------------


class TestValidateLoginResponse:
    """Cover the four branches: error-field rejection, cookie-or-success gate."""

    def test_body_none_no_cookie_raises(self) -> None:
        # No JSON body, no Set-Cookie → cannot confirm success → reject.
        resp = _make_login_response(with_cookie=False)
        with pytest.raises(GribsAuthError, match="did not set a session cookie"):
            GribsClient._validate_login_response(resp, None, _EMAIL)

    def test_body_true_no_cookie_does_not_raise(self) -> None:
        # Truthy non-dict body → `_looks_like_success` returns True → accept
        # even without a cookie (mirrors gribs' 15-byte "1" success status).
        resp = _make_login_response(with_cookie=False)
        GribsClient._validate_login_response(resp, True, _EMAIL)  # must not raise

    def test_body_dict_error_string_false_no_cookie_raises(self) -> None:
        # `error="false"` is in the falsy list, so the error-field branch is
        # skipped; but no cookie + `_looks_like_success({"error": "false"})`
        # is False → falls through to the cookie/success gate and raises.
        resp = _make_login_response(with_cookie=False)
        with pytest.raises(GribsAuthError, match="did not set a session cookie"):
            GribsClient._validate_login_response(resp, {"error": "false"}, _EMAIL)

    def test_body_dict_error_truthy_string_raises_rejected(self) -> None:
        # Non-falsy error string → rejected at the error-field branch.
        resp = _make_login_response(with_cookie=False)
        body = {"error": "bad password"}
        with pytest.raises(GribsAuthError, match="Login rejected"):
            GribsClient._validate_login_response(resp, body, _EMAIL)

    def test_body_with_non_ascii_error_truncated_without_crash(self) -> None:
        # Truncation must handle unicode in the error message body without
        # raising. The error field is non-falsy so we still reject.
        resp = _make_login_response(with_cookie=False)
        body: dict[str, Any] = {"error": "fáîlürê 😱"}
        with pytest.raises(GribsAuthError) as excinfo:
            GribsClient._validate_login_response(resp, body, _EMAIL)
        # Unicode survives into the error message.
        assert "fáîlürê" in str(excinfo.value)

    def test_body_none_with_cookie_does_not_raise(self) -> None:
        # Session cookie set → success regardless of body.
        resp = _make_login_response(with_cookie=True)
        GribsClient._validate_login_response(resp, None, _EMAIL)  # must not raise

    def test_body_dict_no_error_with_cookie_does_not_raise(self) -> None:
        resp = _make_login_response(with_cookie=True)
        GribsClient._validate_login_response(resp, {"error": None}, _EMAIL)

    def test_body_dict_error_false_with_cookie_does_not_raise(self) -> None:
        # `error=False` is in the falsy list → not rejected; cookie set → ok.
        resp = _make_login_response(with_cookie=True)
        GribsClient._validate_login_response(resp, {"error": False}, _EMAIL)

    def test_body_dict_error_zero_with_cookie_does_not_raise(self) -> None:
        # `error=0` is in the falsy list → not rejected; cookie set → ok.
        resp = _make_login_response(with_cookie=True)
        GribsClient._validate_login_response(resp, {"error": 0}, _EMAIL)

    def test_body_dict_error_string_zero_with_cookie_does_not_raise(self) -> None:
        # `error="0"` is in the falsy list → not rejected; cookie set → ok.
        resp = _make_login_response(with_cookie=True)
        GribsClient._validate_login_response(resp, {"error": "0"}, _EMAIL)


# ---------------------------------------------------------------------------
# _serialize_cookies
# ---------------------------------------------------------------------------


class TestSerializeCookies:
    """Verify the contract: never crashes on missing fields, always returns
    dicts with all five keys (name, value, domain, path, expires).
    """

    def test_empty_jar_returns_empty_list(self) -> None:
        assert _serialize_cookies(httpx.Cookies()) == []

    def test_cookie_without_domain_falls_back_to_www_gribs_net(self) -> None:
        # httpx.Cookies.set with no domain yields cookie.domain == '' (falsy).
        # The helper must substitute the gribs.net base domain, never crash.
        jar = httpx.Cookies()
        jar.set("session", "abc")
        entries = _serialize_cookies(jar)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["name"] == "session"
        assert entry["value"] == "abc"
        assert entry["domain"] == "www.gribs.net"
        assert entry["path"] == "/"
        assert entry["expires"] is None

    def test_cookie_with_explicit_domain_and_path_preserved(self) -> None:
        jar = httpx.Cookies()
        jar.set("session", "abc", domain="gribs.net", path="/members")
        entries = _serialize_cookies(jar)
        assert entries[0]["domain"] == "gribs.net"
        assert entries[0]["path"] == "/members"

    def test_cookie_with_empty_domain_and_path_falls_back(self) -> None:
        # httpx lets you set domain='' / path='' explicitly; both are falsy
        # so the helper must apply the same fallback as the None case.
        jar = httpx.Cookies()
        jar.set("session", "abc", domain="", path="")
        entries = _serialize_cookies(jar)
        assert entries[0]["domain"] == "www.gribs.net"
        assert entries[0]["path"] == "/"

    def test_cookie_with_none_value_normalized_to_empty_string(self) -> None:
        # The helper reads `cookie.value or ""` — a None value becomes "".
        # We have to mutate the jar cookie directly because httpx.Cookies.set
        # requires a string value.
        jar = httpx.Cookies()
        jar.set("session", "abc", domain="gribs.net", path="/")
        for c in jar.jar:
            c.value = None
        entries = _serialize_cookies(jar)
        assert entries[0]["value"] == ""

    def test_cookie_with_expires_preserved(self) -> None:
        # httpx.Cookies.set doesn't accept `expires=`; mutate the jar cookie.
        jar = httpx.Cookies()
        jar.set("session", "abc", domain="gribs.net", path="/")
        for c in jar.jar:
            c.expires = 1_700_000_000
        entries = _serialize_cookies(jar)
        assert entries[0]["expires"] == 1_700_000_000

    def test_all_entries_contain_exactly_five_keys(self) -> None:
        # Contract: every entry is a dict with exactly {name, value, domain,
        # path, expires} — no more, no less.
        jar = httpx.Cookies()
        jar.set("a", "1", domain="gribs.net", path="/")
        jar.set("b", "2", domain="gribs.net", path="/members")
        entries = _serialize_cookies(jar)
        assert len(entries) == 2
        for entry in entries:
            assert set(entry.keys()) == {"name", "value", "domain", "path", "expires"}

    def test_multiple_cookies_all_serialized(self) -> None:
        jar = httpx.Cookies()
        jar.set("a", "1", domain="gribs.net", path="/")
        jar.set("b", "2", domain="gribs.net", path="/")
        jar.set("c", "3", domain="gribs.net", path="/")
        entries = _serialize_cookies(jar)
        assert {e["name"] for e in entries} == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# _looks_like_success
# ---------------------------------------------------------------------------


class TestLooksLikeSuccess:
    """Heuristic mapping of non-JSON / best-effort JSON bodies to bool."""

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            (None, False),
            (True, True),
            (False, False),
            (1, True),
            (0, False),
            (-1, False),  # negative ints are not > 0
            (3.14, True),
            (0.0, False),
            ("1", True),
            ("true", True),
            ("ok", True),
            ("OK", True),
            ("success", True),
            ("0", False),
            ("false", False),
            ("", False),
            ("  true  ", True),  # stripped before match
            ({"error": False}, False),  # dict falls through to return False
            ({"error": "false"}, False),
            ({"error": "something"}, False),
            ([1, 2, 3], False),  # non-dict, non-scalar → return False
        ],
    )
    def test_body_mapping(self, body: Any, expected: bool) -> None:
        assert _looks_like_success(body) is expected


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------


class TestTruncate:
    """Safe repr-based truncation for logging/error messages (Issue #2)."""

    def test_short_string_unchanged(self) -> None:
        assert _truncate("hello") == "hello"

    def test_long_string_truncated_with_suffix(self) -> None:
        text = "a" * 250
        result = _truncate(text)
        assert len(result) == 200 + len("...(truncated)")
        assert result.startswith("a" * 200)
        assert result.endswith("...(truncated)")

    def test_string_at_limit_unchanged(self) -> None:
        text = "a" * 200
        assert _truncate(text) == text

    def test_string_over_limit_by_one_truncates(self) -> None:
        text = "a" * 201
        result = _truncate(text)
        assert result.endswith("...(truncated)")
        assert result[:200] == "a" * 200

    def test_none_uses_repr_short(self) -> None:
        # repr(None) == 'None', which is short → returned as-is.
        assert _truncate(None) == "None"

    def test_dict_uses_repr(self) -> None:
        result = _truncate({"a": 1})
        assert result == "{'a': 1}"

    def test_large_dict_truncated(self) -> None:
        body = {f"key_{i}": i for i in range(50)}
        result = _truncate(body)
        assert result.endswith("...(truncated)")
        assert len(result) == 200 + len("...(truncated)")

    def test_non_ascii_unicode_handled(self) -> None:
        # Strings are passed through (not repr'd), unicode preserved.
        assert _truncate("üñîçødé") == "üñîçødé"

    def test_non_ascii_unicode_long_truncated(self) -> None:
        text = "ü" * 250
        result = _truncate(text)
        assert result.endswith("...(truncated)")
        assert result[:200] == "ü" * 200

    def test_custom_limit(self) -> None:
        # Limit applies to the (possibly repr'd) text length.
        assert _truncate("hello", limit=3) == "hel...(truncated)"

    def test_custom_limit_at_boundary(self) -> None:
        assert _truncate("hello", limit=5) == "hello"

    def test_int_uses_repr(self) -> None:
        # Non-string input goes through repr().
        assert _truncate(42) == "42"

    def test_list_uses_repr(self) -> None:
        assert _truncate([1, 2, 3]) == "[1, 2, 3]"


# ---------------------------------------------------------------------------
# _clamp
# ---------------------------------------------------------------------------


class TestClamp:
    """Clamp a limit to [1, max_value]; never raises."""

    @pytest.mark.parametrize(
        ("limit", "max_value", "expected"),
        [
            # Below floor
            (0, 50, 1),
            (-1, 50, 1),
            (-100, 50, 1),
            # At floor
            (1, 50, 1),
            # Mid-range passes through
            (25, 50, 25),
            (50, 100, 50),
            # At ceiling
            (50, 50, 50),
            # Above ceiling clamped down
            (51, 50, 50),
            (100, 50, 50),
            (1_000_000, 50, 50),
            # max_value == 1 (degenerate but legal)
            (0, 1, 1),
            (1, 1, 1),
            (5, 1, 1),
        ],
    )
    def test_clamp_values(self, limit: int, max_value: int, expected: int) -> None:
        assert _clamp(limit, max_value) == expected


# ---------------------------------------------------------------------------
# _resolve_category_id
# ---------------------------------------------------------------------------


class TestResolveCategoryId:
    """Map category name → numeric id, raising on unknown/unverified."""

    def test_verified_antragsboerse_returns_1(self) -> None:
        assert _resolve_category_id("Antragsbörse") == 1

    @pytest.mark.parametrize(
        "name",
        [
            "Arbeit im Rat",
            "Mitgliederbriefe",
            "DenkWerkstatt",
            "Mitgliederversammlungen",
            "Kommunalwahl",
        ],
    )
    def test_other_verified_categories_resolve(self, name: str) -> None:
        # All registered verified categories have a non-None cat_id.
        assert _resolve_category_id(name) == CATEGORY_IDS[name]
        assert CATEGORY_IDS[name] is not None

    def test_registered_wissenswert_resolves_to_2(self) -> None:
        # Wissenswert was previously registered as None (unverified) but
        # live-verification on 2026-07-18 confirmed cat_id=2 via the
        # /members/structure response (`structexp('m_cat_2',{cat:2},...)`).
        assert "Wissenswert" in CATEGORY_IDS
        assert CATEGORY_IDS["Wissenswert"] == 2
        assert _resolve_category_id("Wissenswert") == 2

    def test_unknown_category_raises_with_known_list(self) -> None:
        with pytest.raises(GribsApiError, match="Unknown category 'Unknown'"):
            _resolve_category_id("Unknown")

    def test_unknown_category_error_message_lists_known(self) -> None:
        # The error message should include the formatted known-categories list
        # so callers can self-correct.
        with pytest.raises(GribsApiError) as excinfo:
            _resolve_category_id("Nonexistent")
        msg = str(excinfo.value)
        assert "Antragsbörse (verified)" in msg
        assert "Wissenswert (verified)" in msg

    def test_case_sensitive(self) -> None:
        # Category names contain umlauts and mixed case; lookup is exact.
        with pytest.raises(GribsApiError):
            _resolve_category_id("antragsbörse")  # lowercase 'a'
        with pytest.raises(GribsApiError):
            _resolve_category_id("ANTRAGSBÖRSE")


# ---------------------------------------------------------------------------
# _format_known_categories
# ---------------------------------------------------------------------------


class TestFormatKnownCategories:
    """Verify all categories appear with correct verification status, sorted."""

    def test_returns_list_of_strings(self) -> None:
        result = _format_known_categories()
        assert isinstance(result, list)
        assert all(isinstance(line, str) for line in result)

    def test_contains_all_registered_categories(self) -> None:
        result = _format_known_categories()
        for name in CATEGORY_IDS:
            assert any(line.startswith(name) for line in result), (
                f"{name!r} missing from formatted list"
            )

    def test_antragsboerse_marked_verified(self) -> None:
        result = _format_known_categories()
        assert "Antragsbörse (verified)" in result

    def test_wissenswert_marked_verified(self) -> None:
        result = _format_known_categories()
        assert "Wissenswert (verified)" in result

    def test_verified_status_matches_category_ids(self) -> None:
        # Cross-check: every entry's status matches the None-ness of its cat_id.
        result = _format_known_categories()
        for name, cat_id in CATEGORY_IDS.items():
            expected_status = "verified" if cat_id is not None else "unverified"
            matching = [line for line in result if line.startswith(name)]
            assert len(matching) == 1
            assert matching[0] == f"{name} ({expected_status})"

    def test_result_is_sorted_alphabetically(self) -> None:
        result = _format_known_categories()
        assert result == sorted(result)

    def test_format_is_name_paren_status(self) -> None:
        # Every entry matches the pattern "<name> (<status>)".
        result = _format_known_categories()
        for line in result:
            assert line.endswith(" (verified)") or line.endswith(" (unverified)")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
