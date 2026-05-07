"""Model Context Protocol (MCP) server wrapper.

Wraps the localaiproject dispatcher as an MCP tool source so any
MCP-aware client (Claude Code, OpenCode, Cursor, future agents) can
drive a localaiproject session out of the box.

Phase 1 ships stdio transport + a curated subset of dispatcher
commands as MCP tools + a single ``sessions://list`` resource. See
``docs/design/mcp-server.md`` for the full design.

Run as a server::

    python -m app.mcp                # stdio transport (default)

Or from inside the dispatcher (started in-process for local agents)::

    > mcp serve                      # blocks the terminal; Ctrl-C exits
"""
