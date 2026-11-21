"""Privyx for MCP over stdio: a server run behind Privyx, and Privyx's own tools.

:func:`wrap` masks what an MCP server sends to its client; :func:`serve`
answers as an MCP server whose tools mask, detect, and check diffs through the
app's services.
"""

from privyx.mcp._serve import serve
from privyx.mcp._wrap import wrap

__all__ = ["serve", "wrap"]
