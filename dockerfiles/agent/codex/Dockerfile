# cyberarena/agent-codex — OpenAI Codex CLI (+ the routed codex-security scanner) on
# top of cyberarena/agent-base.
# Build: docker build -t cyberarena/agent-codex dockerfiles/agent/codex
ARG BASE_IMAGE=cyberarena/agent-base
FROM ${BASE_IMAGE}

# Codex CLI. Codex still writes its per-team config.toml/auth at runtime; only the
# binary comes from here.
# PINNED to a concrete version (never the floating `latest` tag) — keep in sync with
# CODEX_VERSION in agents/codex.py. 0.146.0 no longer ships a `gpt-5.3-codex` model
# slug; see the CODEX_VERSION comment for what that costs configs still naming it.
RUN npm install -g @openai/codex@0.146.0

# Codex Security — OpenAI's security-scan CLI (`codex-security scan|scans|findings`).
# NOT an AgentManager provider: it has no prompt channel and no session resume, so it is
# baked as a TOOL the agent invokes itself (alongside nmap/sqlmap), not as a harness.
# It lives in THIS image (not agent-base) because only the codex adapter wires it up
# (agents/codex.py::_build_config_toml, gated by `agent.security_scanner: true`).
# It vendors its OWN @openai/codex (0.144.6) under its node_modules, which does NOT
# disturb the pinned global codex above — verified: global stays 0.146.0.
#
# npm installs the real binary to /usr/bin/codex-security. Bare, it is inert in an arena
# container: it ignores the ambient $CODEX_HOME/config.toml and POSTs straight to
# api.openai.com — unmetered, outside every per-team budget, and denied by the egress
# firewall. The wrapper below shadows it so the routed path is the default.
RUN npm install -g @openai/codex-security@0.1.5

# The routed front door. SAME NAME as the real binary, installed to /usr/local/bin, which
# precedes /usr/bin on PATH — so an agent that runs `codex-security` gets this wrapper (which
# owns credential resolution, the api-router overrides, and exit-code semantics) and cannot
# forget a flag and silently fall back to direct-OpenAI. The wrapper calls the real binary by
# its absolute path /usr/bin/codex-security, so it does not recurse into itself.
COPY codex-security /usr/local/bin/codex-security
RUN chmod +x /usr/local/bin/codex-security

# A local stdio MCP server wrapping the routed `codex-security`, so the scanner reaches the
# model as an advertised TOOL (auto-discovered, and recorded as `mcp_tool_call` in the
# trajectory) rather than as a PATH binary nothing announces. Wired per team by
# agents/codex.py::_build_config_toml when `agent.security_scanner: true`; inert otherwise.
COPY codex-security-mcp /usr/local/bin/codex-security-mcp
RUN chmod +x /usr/local/bin/codex-security-mcp

# Drop the npm cache. It used to be load-bearing — the orchestrator re-ran `npm i -g` per team
# at game start and the baked cache made that offline/fast — but runtime CLI install is gone
# (install() only preflights now), so the cache is pure dead weight. Removing it also closes an
# escape hatch: with the cache present, a root agent can `npm install -g @openai/codex-security`
# to recover the scanner OFFLINE (no network needed) even after it is removed. Verified: cache
# present -> reinstalls under a dead proxy; cache cleared + no network -> cannot. (This closes
# only the OFFLINE vector; the ONLINE one needs registry.npmjs.org dropped from the egress
# allow-list, which is now also dead weight — see the firewall block in the game configs.)
RUN npm cache clean --force && rm -rf /root/.npm

# Fail the build early if anything is missing from PATH. The real scanner is version-checked
# by its ABSOLUTE path (the bare name is the wrapper now); the wrapper is checked for its
# refusal path — `codex-security scan` in a keyless build shell must exit non-zero rather
# than fall back to an unrouted call, which proves the wrapper is on PATH ahead of the real
# binary AND fails loud.
RUN codex --version \
    && /usr/bin/codex-security --version \
    && ! codex-security scan /tmp 2>/dev/null
