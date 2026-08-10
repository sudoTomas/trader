"""MCP surface (streamable HTTP at /mcp) — thin adapter over the service registry.

Agentic-first: MCP tools are generated from the same capability registry the API and CLI use.
There is deliberately no raw place-order tool — orders exist only through the RiskGuarded loop
(docs/architecture.md §Surfaces).
"""

from __future__ import annotations

from mcp.server import MCPServer

from trader.surfaces.services import ServiceRegistry


def build_mcp(registry: ServiceRegistry) -> MCPServer:
    """One MCP tool per registered capability, named by kind (get_* for queries)."""
    mcp: MCPServer = MCPServer("trader")
    for cap in registry.all():
        mcp.tool(name=cap.tool_name, description=cap.summary)(cap.handler)
    return mcp
