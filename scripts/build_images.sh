#!/usr/bin/env bash
# Pre-build the v3 infrastructure / base images. Run once on a fresh host;
# idempotent (Docker's layer cache makes re-runs cheap). The PER-GAME images —
# the challenge images + the per-game flag-service / health-poller — are built by
# `main.py start` itself (challenges.registry.build_images + facility.build), so
# adding a challenge does NOT require touching this script.
#
# After this:  python main.py start -c config/<game>.yaml      (no `setup` step)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Agent containers. Layered: ONE shared base (ubuntu + pentest/dev tooling + Node +
# uv + proxy-aware entrypoint, no harness CLI) and then one thin image per harness
# on top of it — cyberarena/agent-{claude,codex,opencode,gemini,qwen,kimi,grok,
# deepseek,nexau,openhands}. A game.yaml dev node names the image for its team's
# `agent.provider`; script/idle teams use agent-base itself. Build a subset with
#   AGENT_HARNESSES="claude codex" bash scripts/build_images.sh
# (the base is always built — the harness images are FROM it).
echo "==> building cyberarena/agent-base"
docker build -t cyberarena/agent-base dockerfiles/agent/base

if [ -n "${AGENT_HARNESSES:-}" ]; then
  harnesses="$AGENT_HARNESSES"
else
  harnesses="$(find dockerfiles/agent -mindepth 1 -maxdepth 1 -type d ! -name base -printf '%f\n' | sort)"
fi
for h in $harnesses; do
  [ -f "dockerfiles/agent/$h/Dockerfile" ] || { echo "!! no dockerfiles/agent/$h/Dockerfile" >&2; exit 1; }
  echo "==> building cyberarena/agent-$h"
  docker build -t "cyberarena/agent-$h" "dockerfiles/agent/$h"
done

# On-net MCP gateway — the only agent-reachable control door.
echo "==> building cyberarena/mcp-gateway"
docker build -t cyberarena/mcp-gateway -f dockerfiles/mcp-gateway/Dockerfile .

# Facility BASE images. `main.py start` layers each game's challenge probes on
# top of these to produce cyberarena/{flag-service,health-poller}.
echo "==> building cyberarena/flag-service-base"
docker build -t cyberarena/flag-service-base -f dockerfiles/flag-service/Dockerfile .
echo "==> building cyberarena/health-poller-base"
docker build -t cyberarena/health-poller-base -f dockerfiles/health-poller/Dockerfile .

# api-router: base image, then a baked config layer. The egress firewall lives in
# apirouter-data/config.yaml — deployment-specific + gitignored, so the -cfg image
# can only be built once you've supplied that config.
echo "==> building cyberarena/api-router"
docker build -t cyberarena/api-router apirouter

if [ -f apirouter-data/config.yaml ]; then
  # Dockerfile.baked is tiny + gitignored; create it if it doesn't exist yet.
  [ -f apirouter-data/Dockerfile.baked ] || \
    printf 'FROM cyberarena/api-router\nCOPY config.yaml /data/config.yaml\n' > apirouter-data/Dockerfile.baked
  echo "==> building cyberarena/api-router-cfg"
  docker build -t cyberarena/api-router-cfg -f apirouter-data/Dockerfile.baked apirouter-data
else
  echo "!! SKIPPED cyberarena/api-router-cfg — apirouter-data/config.yaml not found."
  echo "   Supply the firewall config, then re-run this script:"
  echo "     mkdir -p apirouter-data && cp apirouter/config.example.yaml apirouter-data/config.yaml"
  echo "     \$EDITOR apirouter-data/config.yaml   # set the egress allow-list"
fi

echo "==> done"
docker images | grep cyberarena
