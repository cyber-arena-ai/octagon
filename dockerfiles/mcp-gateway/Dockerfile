# Containerized MCP server (v3): agents reach MCP directly on their network,
# with no api-router relay. Each cyberarena tool is a thin HTTP client —
# submit_flag goes straight to the flag-service; the GameMgr-coupled tools
# (status / attacks / report / restart) forward to GameMgr REST, which still
# executes privileged ops (docker restart stays host-side). No docker.sock.
#
# Build from the REPO ROOT (reuses gamemgr.mcp_server's tool definitions):
#   docker build -t cyberarena/mcp-gateway -f dockerfiles/mcp-gateway/Dockerfile .
#
# Run (wiring via env):
#   docker run -d --name mcp_gateway -p 127.0.0.1:9301:8080 \
#     -e CYBERARENA_GM_BASE_URL=http://host.docker.internal:8000 \
#     -e CYBERARENA_FLAG_SERVICE_URL=http://flag_service:8080 \
#     cyberarena/mcp-gateway
# Agents point mcp_endpoint at  http://<gateway>:8080/mcp/
FROM --platform=linux/amd64 python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY dockerfiles/mcp-gateway/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# The gateway reuses gamemgr.mcp_server's tool defs + backend errors; that
# module imports only the `mcp` SDK at runtime (GameMgr is TYPE_CHECKING-only),
# so the slim slice of gamemgr/ is self-contained (__init__.py is empty).
COPY mcpgateway/ /app/mcpgateway/
COPY gamemgr/__init__.py /app/gamemgr/__init__.py
COPY gamemgr/mcp_server.py /app/gamemgr/mcp_server.py

EXPOSE 8080

CMD ["uvicorn", "mcpgateway.main:app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
