"""gribs_mcp — MCP server for gribs.net (Antragsbörse, Vorlagenpool, Dateibereich).

Grünen-interne, login-pflichtige Plattform. Der Server wird via stdio transport
in OpenCode/Agenten-Harnesses eingebunden. Authentication is handled via cached
session cookies stored in the OS keyring.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
