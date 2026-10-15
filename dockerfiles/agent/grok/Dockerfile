# cyberarena/agent-grok — xAI "Grok Build" CLI on top of cyberarena/agent-base.
# Build: docker build -t cyberarena/agent-grok dockerfiles/agent/grok
ARG BASE_IMAGE=cyberarena/agent-base
FROM ${BASE_IMAGE}

# xAI "Grok Build" CLI (official, closed-source). Baked as the SOLE install path —
# GrokAgentManager.install verifies this pin via verify_cli and does NO runtime npm
# install (a runtime `npm i -g` would re-pull a possibly-different build of this beta
# CLI at game time, defeating the bake); it only writes its per-team
# ~/.grok/config.toml (BYOK model + MCP) + grok.env. PINNED —
# keep in sync with GROK_VERSION in agents/grok.py; the CLI is early-access beta with a
# very high release cadence, and the adapter runs it with --no-auto-update. Node >=20
# (satisfied by agent-base's Node). npm cache dropped in the same layer (dead weight;
# see the codex image for the rationale).
RUN npm install -g @xai-official/grok@0.2.111 \
    && npm cache clean --force && rm -rf /root/.npm

RUN grok --version
