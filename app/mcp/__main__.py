"""Make the MCP server runnable as ``python -m app.mcp``."""
from app.mcp.server import main


if __name__ == "__main__":
    raise SystemExit(main())
