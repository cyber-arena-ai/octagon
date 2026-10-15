"""mcp-gateway — the containerized MCP server (v3).

Reuses the cyberarena tool definitions from gamemgr.mcp_server, bound to HTTP
backends instead of an in-process GameMgr, so an isolated agent reaches MCP
directly on its network (no api-router relay). See
docs/archived/facility_direct_exec_plan_v3.md.
"""
