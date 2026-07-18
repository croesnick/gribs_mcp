"""Keyring-based credential + cookie caching for gribs.net.

Login flow (per INTENT.md §"Login-Flow"):
- POST https://www.gribs.net/users/ajax_login
- Body: form-urlencoded `email=…&password=…&keep=true`
- Header: `X-Requested-With: XMLHttpRequest`
- No SSO, no JS handshake, no CSRF token — pure httpx form-POST.

Credentials and cookies are persisted in the OS keyring under service name
`gribs_mcp`. Env-vars `GRIBS_EMAIL` / `GRIBS_PASSWORD` provide a fallback for
CI / headless setups.

`python -m gribs_mcp.auth` exposes an interactive CLI helper that prompts for
email/password via `getpass`, stores them in the keyring, and optionally
performs a test login.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

import keyring

SERVICE_NAME = "gribs_mcp"
CREDENTIAL_KEY = "credentials"
COOKIE_KEY = "cookies"

COOKIE_MAX_AGE: timedelta = timedelta(days=5)


class CookieEntry(TypedDict, total=False):
    """JSON-serialized shape of a single cookie in the cache.

    This is the de-facto public serialization contract between `auth.py`
    (which persists cookies) and `client.py` (which reads them back into the
    httpx cookie jar). Both modules import this name.
    """

    name: str
    value: str
    domain: str
    path: str
    expires: float | None


class CookieCache(TypedDict):
    """JSON-serialized shape of the cookie cache."""

    created_at: str
    cookies: list[CookieEntry]


@dataclass(frozen=True)
class Credentials:
    """User credentials for gribs.net login."""

    email: str
    password: str


class AuthError(RuntimeError):
    """Raised when credentials are missing or invalid."""


def _now() -> datetime:
    return datetime.now(UTC)


def load_credentials() -> Credentials:
    """Load credentials from keyring, falling back to env-vars.

    Raises:
        AuthError: if no credentials are available.
    """
    raw = keyring.get_password(SERVICE_NAME, CREDENTIAL_KEY)
    if raw:
        try:
            data: dict[str, Any] = json.loads(raw)
            if data.get("email") and data.get("password"):
                return Credentials(
                    email=str(data["email"]), password=str(data["password"])
                )
        except (json.JSONDecodeError, AttributeError) as exc:
            raise AuthError(f"Stored credentials are malformed: {exc}") from exc

    email = os.environ.get("GRIBS_EMAIL")
    password = os.environ.get("GRIBS_PASSWORD")
    if email and password:
        return Credentials(email=email, password=password)

    raise AuthError(
        "No gribs credentials found. Store them via `python -m gribs_mcp.auth` "
        "or set GRIBS_EMAIL / GRIBS_PASSWORD environment variables."
    )


def store_credentials(email: str, password: str) -> None:
    """Persist credentials to the OS keyring."""
    payload = json.dumps({"email": email, "password": password})
    keyring.set_password(SERVICE_NAME, CREDENTIAL_KEY, payload)


def delete_credentials() -> None:
    """Remove stored credentials (no-op if absent)."""
    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(SERVICE_NAME, CREDENTIAL_KEY)


def load_cookies() -> list[CookieEntry] | None:
    """Load cached cookies from the keyring if still fresh.

    Two freshness checks apply:
    1. Cache age: `created_at` must be within `COOKIE_MAX_AGE` (5 days).
    2. Cookie expiry: any cookie with an `expires` field in the past causes
       the whole cache to be treated as stale (so the client re-logs in).

    Returns:
        The cached cookie list if present and fresh, otherwise None.
    """
    raw = keyring.get_password(SERVICE_NAME, COOKIE_KEY)
    if not raw:
        return None
    try:
        cache: CookieCache = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    created_at_raw = cache.get("created_at")
    if not created_at_raw:
        return None
    try:
        created_at = datetime.fromisoformat(created_at_raw)
    except ValueError:
        return None

    if _now() - created_at > COOKIE_MAX_AGE:
        return None

    cookies = cache.get("cookies")
    if not isinstance(cookies, list):
        return None

    # Cookie-level expiry check: if any session cookie is past its `expires`,
    # treat the whole cache as stale so the client re-logs in.
    now_ts = _now().timestamp()
    for entry in cookies:
        expires = entry.get("expires")
        if expires is not None and expires > 0 and expires < now_ts:
            return None

    return cookies


def store_cookies(cookies: list[CookieEntry]) -> None:
    """Persist cookies to the keyring with a fresh `created_at` timestamp."""
    cache: CookieCache = {
        "created_at": _now().isoformat(),
        "cookies": cookies,
    }
    keyring.set_password(SERVICE_NAME, COOKIE_KEY, json.dumps(cache))


def delete_cookies() -> None:
    """Remove cached cookies (no-op if absent)."""
    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(SERVICE_NAME, COOKIE_KEY)


def _interactive() -> None:
    """Interactive CLI helper: prompt for credentials, store, optionally test login."""
    print("gribs_mcp credential helper")
    print("Press Ctrl+C to cancel.\n")

    email = input("Email: ").strip()
    if not email:
        print("Email is required.")
        raise SystemExit(1)

    password = getpass.getpass("Password: ")
    if not password:
        print("Password is required.")
        raise SystemExit(1)

    store_credentials(email, password)
    print(f"\nCredentials stored in keyring under service '{SERVICE_NAME}'.")

    answer = input("\nPerform a test login now? [y/N]: ").strip().lower()
    if answer in {"y", "yes"}:
        # Local import to avoid a hard client.py -> auth.py -> client.py cycle at
        # module load time and to keep the CLI helper self-contained.
        from gribs_mcp.client import GribsClient

        async def _run() -> None:
            client = GribsClient()
            await client.login(email, password)
            await client.aclose()
            print("Login successful. Cookies cached.")

        try:
            asyncio.run(_run())
        except Exception as exc:  # noqa: BLE001 — surface any failure to the user
            print(f"Login failed: {exc}")
            raise SystemExit(2) from exc


if __name__ == "__main__":
    _interactive()
