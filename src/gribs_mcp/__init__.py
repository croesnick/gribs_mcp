"""gribs_mcp — MCP server for gribs.net (Antragsbörse, Vorlagenpool, Dateibereich).

Grünen-interne, login-pflichtige Plattform. Der Server wird via stdio transport
in OpenCode/Agenten-Harnesses eingebunden. Authentication is handled via cached
session cookies stored in the OS keyring.
"""

from __future__ import annotations

import logging

# Library best practice: don't force logging config on the host app. Add a
# NullHandler so that if the host app doesn't configure logging, our log
# records are silently dropped rather than emitting "No handlers found" warnings.
logging.getLogger(__name__).addHandler(logging.NullHandler())

# FastMCP stdio constraint: httpx logs at INFO level = one line per request,
# which goes to stderr. Under stdio MCP hosts, stderr noise can cause
# back-pressure deadlocks when the host's stderr reader can't keep up.
# Quarantine httpx and httpcore to WARNING+ to prevent this.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

__version__ = "0.1.0"
__all__ = ["__version__"]
