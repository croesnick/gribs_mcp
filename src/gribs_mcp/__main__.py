"""Entry point: run the gribs_mcp MCP server over stdio transport."""

from __future__ import annotations

import logging

from gribs_mcp.server import mcp


def main() -> None:
    """Run the FastMCP server on stdio.

    FastMCP stdio: httpx logs at INFO = one line per request → stderr noise →
    back-pressure deadlocks in MCP stdio hosts. Quarantine to WARNING+ at
    entry-point time (NOT at import time) so library consumers can keep
    httpx INFO logs if they want them.
    """
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
