# cyberarena/agent-opencode — OpenCode CLI on top of cyberarena/agent-base.
# Build: docker build -t cyberarena/agent-opencode dockerfiles/agent/opencode
ARG BASE_IMAGE=cyberarena/agent-base
FROM ${BASE_IMAGE}

# OpenCode CLI. Unpinned — the only agent CLI without a version pin to check
# against (see NPM_PACKAGE in agents/opencode.py). npm cache dropped in the same
# layer (dead weight; see the codex image for the offline-reinstall rationale).
RUN npm install -g opencode-ai \
    && npm cache clean --force && rm -rf /root/.npm

RUN opencode --version
