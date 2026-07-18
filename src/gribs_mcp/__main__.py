"""Entry point: run the gribs_mcp MCP server over stdio transport."""

from __future__ import annotations

from gribs_mcp.server import mcp


def main() -> None:
    """Run the FastMCP server on stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
