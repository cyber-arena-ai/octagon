# cyberarena/agent-qwen — Qwen Code CLI on top of cyberarena/agent-base.
# Build: docker build -t cyberarena/agent-qwen dockerfiles/agent/qwen
ARG BASE_IMAGE=cyberarena/agent-base
FROM ${BASE_IMAGE}

# Qwen Code CLI (gemini-cli fork). Baked as the SOLE install path —
# QwenCodeAgentManager.install verifies this pin via verify_cli and does NO
# runtime npm install; it only writes per-team settings.json (remote MCP) + qwen.env.
# PINNED — keep in sync with QWEN_VERSION in agents/qwen.py. Unpinned latest tracks
# npm and the project ships 500+ releases (very churny), so a silent bump could
# change flag/config surface mid-match. Node >=22 (satisfied by agent-base's Node).
# npm cache dropped in the same layer (dead weight; see the codex image).
RUN npm install -g @qwen-code/qwen-code@0.20.1 \
    && npm cache clean --force && rm -rf /root/.npm

RUN qwen --version
