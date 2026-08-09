"""MCP surface (FastMCP, streamable HTTP at /mcp) — thin adapter over the service registry.

Agentic-first: MCP tools are generated from the same capability registry the API and CLI use.
There is deliberately no raw place-order tool — orders exist only through the RiskGuarded loop
(docs/architecture.md §Surfaces).
"""

from __future__ import annotations

from mcp.server import MCPServer

from trader.surfaces.services import build_registry


def build_mcp() -> MCPServer:
    registry = build_registry()
    mcp: MCPServer = MCPServer("trader")

    @mcp.tool(name="get_health", description="Liveness and build status.")
    async def get_health() -> dict:
        return await registry.get("health").handler()

    return mcp


mcp = build_mcp()
