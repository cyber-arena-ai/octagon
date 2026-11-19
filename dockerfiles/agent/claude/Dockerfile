# cyberarena/agent-claude — Claude Code on top of cyberarena/agent-base.
# Build: docker build -t cyberarena/agent-claude dockerfiles/agent/claude
# (agent-base must exist first; scripts/build_images.sh orders this.)
ARG BASE_IMAGE=cyberarena/agent-base
FROM ${BASE_IMAGE}

# Claude Code. Installed to /usr/bin via NPM_CONFIG_PREFIX. This is the only place
# it gets installed — ClaudeAgentManager.install just checks it is here.
#
# No claude-code-router. ppapi (our upstream) natively serves Anthropic
# Messages format alongside OpenAI / Gemini for every model — Claude Code can
# call gpt-4o / gemini-2.5-flash through ppapi's /v1/messages with zero local
# translation. If the upstream ever changes to one that ONLY speaks OpenAI
# Chat Completions, add `@musistudio/claude-code-router` here and re-add the
# shim — see docs/UML/agent_manager.md for the pattern.
# PINNED — keep in sync with CLAUDE_CODE_VERSION in agents/claude.py, which holds
# the history: 2.1.207 until 11-19 (2.1.208 crash-looped opus on ppapi's
# redacted_thinking 400s), 2.1.328 since (claude-opus-5-5 needs >= 2.1.280).
#
# The npm cache is dropped in the same layer: runtime CLI install is gone, so it
# is dead weight, and a populated cache lets a root agent re-install packages
# OFFLINE (see the codex image for why that matters).
RUN npm install -g @anthropic-ai/claude-code@2.1.328 \
    && npm cache clean --force && rm -rf /root/.npm

RUN claude --version
