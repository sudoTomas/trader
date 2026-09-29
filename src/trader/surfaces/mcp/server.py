"""MCP surface (streamable HTTP at /mcp, and a capture-scoped subset at /mcp-capture) —
thin adapter over the service registry.

Agentic-first: MCP tools are generated from the same capability registry the API and CLI use.
There is deliberately no raw place-order tool — orders exist only through the RiskGuarded loop
(docs/architecture.md §Surfaces).
"""

from __future__ import annotations

from mcp.server import MCPServer

from trader.surfaces.services import ServiceRegistry

# The capture surface (#8): what an external research agent holding the capture key may
# do. Capability names (registry keys); the tool names derive from them
# (research_notes -> get_research_notes, macro_view -> get_macro_view). Keep in step with
# CAPTURE_ROUTES in surfaces/api/middleware.py.
CAPTURE_CAPABILITIES = frozenset(
    {"submit_research_note", "research_notes", "retire_note", "macro_view"}
)


def build_mcp(
    registry: ServiceRegistry, *, only: frozenset[str] | None = None, name: str = "trader"
) -> MCPServer:
    """One MCP tool per registered capability, named by kind (get_* for queries).

    ``only`` restricts the server to that allowlist of capability names; an unknown name
    is refused outright rather than silently narrowing (or, after a rename, widening)
    the surface.
    """
    if only is not None:
        unknown = only - {cap.name for cap in registry.all()}
        if unknown:
            raise ValueError(f"unknown capability in MCP allowlist: {sorted(unknown)}")
    mcp: MCPServer = MCPServer(name)
    for cap in registry.all():
        if only is not None and cap.name not in only:
            continue
        mcp.tool(name=cap.tool_name, description=cap.summary)(cap.handler)
    return mcp
