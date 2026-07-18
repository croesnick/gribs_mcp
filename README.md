# gribs_mcp

MCP server for [gribs.net](https://www.gribs.net/) — the Grünen-intern (party-internal), login-pflichtige KPV-Bayern platform hosting the **Antragsbörse** (Musteranträge, Beschlüsse, Positionspapiere) and secondary sections like Wissenswert, Mitgliederbriefe, and Mitgliederversammlungen.

This server exposes gribs.net content to AI harnesses (OpenCode) via the [Model Context Protocol](https://modelcontextprotocol.io/), giving an AI assistant read-only access to search and browse Antragsbörse posts and fetch the newest public posts. It is **read-only by design** — no writing, no triage, no uploads — and only surfaces content the running user is already authorized to see (login is handled via the user's own gribs.net credentials, cached locally).

Every politically relevant result carries a source URL and retrieval timestamp (**Quellenpflicht**) so downstream tools can cite where a claim came from and when it was fetched.

## Tools

All tools are `async`, annotated `readOnlyHint=True`, and clamp `limit` parameters to `[1, MAX]`. Every result model carries `url: str` and `retrieved_at: datetime` (UTC).

| Tool | Description | Returns |
|---|---|---|
| `search_antraege(query, category?, whole_word?, l1?, l2?, l3?, limit?)` | Full-text search across a gribs section (default: Antragsbörse). Up to 50 hits. Pass `l1`/`l2`/`l3` to scope to a subcategory. | `list[SearchHit]` — title, snippet, `wp_id`, `url`, `retrieved_at` |
| `get_antrag(post_id)` | Fetch a single post (antrag) in full detail. | `PostDetail` — title, date, `view_count`, `share_url`, `category_breadcrumb`, `body_html`, `body_text`, `url`, `retrieved_at` |
| `resolve_post_id(wp_id)` | Resolve a WordPress `wp_id` (from `search_antraege`) to the internal `post_id` required by `get_antrag`. | `PostIdRef` — `wp_id`, `post_id`, `url`, `retrieved_at` |
| `list_categories(category?)` | Enumerate the top-level (L1) sub-categories within a section (e.g. Umwelt, Soziales, …). | `CategoryNode` — `id`, `label`, `children` (recursive) |
| `list_antraege_in_category(category?, l1?, l2?, l3?, limit?)` | Drill into the category tree without a search query. Returns subcategories for intermediate nodes; leaves return an empty expansion (use `search_antraege` with `l1`/`l2`/`l3` to list posts in a leaf). | `StructureExpansion` — `subcategories` (list[CategoryNode]) or `posts` (list[PostTeaser]) |
| `recent_posts(limit?)` | Fetch the newest posts from the public gribs.net homepage (no login required). Enrichment is concurrent. | `list[PostTeaser]` — `post_id`, title, date, `url`, `retrieved_at` |
| `extract_downloads(post_id)` | Extract download links (PDFs and download-looking anchors) from a post body. | `list[Download]` — `url`, `link_text`, `filename`, `is_pdf`, `source_post_id`, `source_url`, `retrieved_at` |

**Quellenpflicht**: every tool that returns politically relevant data includes `url` + `retrieved_at` on each item. This is enforced at the Pydantic model layer, not by convention.

## Library stack

| Aufgabe | Library | Lizenz |
|---|---|---|
| MCP framework | [`mcp` (FastMCP)](https://github.com/modelcontextprotocol/python-sdk) | MIT |
| HTTP client | [`httpx`](https://www.python-httpx.org/) | BSD-3-Clause |
| Output models | [`pydantic`](https://docs.pydantic.dev/) | MIT |
| HTML parsing (structure) | [`selectolax`](https://github.com/rushter/selectolax) | MIT |
| Article body extraction | [`trafilatura`](https://github.com/adbar/trafilatura) | Apache-2.0 |
| OS credential/cookie storage | [`keyring`](https://github.com/jaraco/keyring) | MIT |

**No Playwright.** gribs.net's simple form-POST login (no SSO, no JS handshake, no CSRF) does not require a browser — pure `httpx` is sufficient and dramatically simpler. There is no `uv run playwright install` step.

## Setup

```bash
# 1. Install dependencies (uv manages a virtualenv automatically).
uv sync

# 2. Store your gribs.net credentials in the OS keyring.
#    This prompts for email + password via getpass (password is not echoed)
#    and optionally performs a test login to verify the credentials work.
uv run python -m gribs_mcp.auth

# 3. (Optional) Smoke-test the server over stdio.
uv run gribs-mcp
```

For CI / headless setups, fall back to environment variables (the keyring is checked first, then env vars):

```bash
export GRIBS_EMAIL="you@example.org"
export GRIBS_PASSWORD="…"
```

Cookies are cached in the OS keyring under service name `gribs_mcp` and are automatically refreshed when they expire (30-day TTL) or when the API returns 401/403. There is no browser profile to manage.

## Configure in OpenCode

Register `gribs-mcp` as a stdio MCP server in your `opencode.json` (analog to `allgaeuer_zeitung_mcp`):

```json
{
  "mcp": {
    "gribs_mcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/gribs_mcp", "gribs-mcp"],
      "enabled": true
    }
  }
}
```

Replace `/path/to/gribs_mcp` with the absolute path to this checkout. The `gribs-mcp` script entry point is defined in `pyproject.toml` (`[project.scripts]`).

## Development

```bash
uv sync                       # install dev deps
uv run ruff check             # lint (E/F/I/UP/B/SIM ruleset)
uv run ruff format --check    # format check
uv run mypy src/              # strict type check
uv run pytest                 # tests (deterministic, no live HTTP)
```

All four gates must pass before merging. Tests use HTML/JSON fixtures under `tests/fixtures/` — no live gribs.net calls, so they run offline and deterministically.

## Architecture

```
src/gribs_mcp/
├── __main__.py   # main() -> mcp.run(transport="stdio")
├── server.py     # FastMCP instance + 7 @mcp.tool definitions (read-only)
├── models.py     # Pydantic v2 models (frozen) with Quellenpflicht fields
├── client.py     # httpx.AsyncClient + cookie jar + auth retry (GribsClient)
├── parsers.py    # Pure HTML/JSON parsers (selectolax + trafilatura)
└── auth.py       # keyring credential + cookie cache (30-day TTL)
```

Key design decisions:

- **httpx-only login** — gribs.net uses a plain form-POST to `/users/ajax_login` with no SSO, no JS handshake, no CSRF token. A browser (Playwright) would be pure overhead. The client does a single `httpx.AsyncClient.post` and captures the `Set-Cookie` session cookie.
- **Keyring cookie cache** — credentials and session cookies are persisted in the OS keyring (service `gribs_mcp`), not on disk. A 30-day TTL plus per-cookie `expires` checks gate freshness; on 401/403 the client clears the cache and re-logs in (serialized by an `asyncio.Lock` to avoid concurrent-login races).
- **JSON vs HTML response handling** — gribs is inconsistent: `/members/structure`, `/members/singlepost`, and `/members/expandStructure` return JSON `{error, ...}` with HTML snippets inside; `/members/search` and `/post/recentposts` return raw HTML. The client has separate `_post_form_json` / `_post_form_html` paths sharing one retry helper.
- **HTML-entity-encoded onclick args** — the live `structexp(...)` calls in `navigation` HTML encode their object's quotes as `&quot;`. Parsers call `html.unescape()` before parsing the JS object (shared `_parse_js_object` helper).
- **Concurrent enrichment** — `recent_posts` fetches the scaffold, then fires all `postWidgetFill` enrichment calls concurrently via `asyncio.gather` (3 parallel requests for the default 3 posts). Order is preserved (gather guarantees positional ordering).
- **Read-only tools + Quellenpflicht** — every tool is annotated `readOnlyHint=True`; every politically relevant output model carries `url: str` + `retrieved_at: datetime` (UTC) at the Pydantic schema level, so a missing source is a type error, not a convention violation.

## Categories (live-verified)

The `category` parameter on `search_antraege`, `list_categories`, and `list_antraege_in_category` accepts these names:

| Category name | cat_id | Status |
|---|---|---|
| `Antragsbörse` | 1 | verified |
| `Wissenswert` | 2 | verified |
| `Arbeit im Rat` | 5 | verified |
| `Mitgliederbriefe` | 6 | verified |
| `DenkWerkstatt` | 7 | verified |
| `Mitgliederversammlungen` | 8 | verified |
| `Kommunalwahl` | 9 | verified |

## Known limitations / roadmap

- **`list_antraege_in_category` on leaf nodes returns an empty expansion** — gribs.net's `/members/expandStructure` returns a *search form* (not post listings) when called on a leaf subcategory. To list posts in a subcategory, call `search_antraege` with the same `l1`/`l2`/`l3` ids and a broad query (e.g. 'a' or 'der' — gribs doesn't support an empty searchstring).
- **`extract_downloads` keyword heuristic** — links are included if the URL ends in `.pdf` OR the anchor text contains a download keyword (download/pdf/antrag/vorlage/beschluss/musterantrag/herunterladen). This may produce false positives (e.g. a non-download link mentioning "antrag") or miss downloads with non-standard anchor text. The `is_pdf` flag distinguishes confirmed PDFs.

## Reverse-engineering notes

Key facts about the gribs.net API (verified live 2026-07-18):

- **Login**: `POST /users/ajax_login` with `email`, `password`, `keep=true`. No SSO, no CSRF. Returns a 15-byte non-JSON status; success is signalled by `Set-Cookie`.
- **API shape**: all calls are POST `application/x-www-form-urlencoded` with header `X-Requested-With: XMLHttpRequest`. Responses are either JSON `{error, ...}` with HTML snippets inside (structure, singlepost, expandStructure) or raw HTML (search, recentposts).
- **ID duality**: posts have an internal `post_id` (needed by `/members/singlepost`) AND a WordPress `wp`-ID (returned by `/members/search` in `?wp=<id>` deep-links), plus an optional `?h=<hash>` share link. Use `resolve_post_id` to bridge `wp_id` → `post_id`.
- **wp_id → post_id resolution**: `GET /?wp=<id>` with `follow_redirects=True` returns the member page at `/members/home/wp-<id>`, which contains `"post_id":"<N>"` in a JSON-ish JS blob.
- **Recent posts**: `/post/recentposts` returns only scaffolds (post_id + spinner); the browser lazy-loads each teaser via `/members/postWidgetFill` with `post_id` + `caller`. The `recent_posts` tool does this concurrently.

## License

MIT (placeholder pending final decision).
