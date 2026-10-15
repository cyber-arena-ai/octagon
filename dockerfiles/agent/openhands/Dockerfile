# cyberarena/agent-openhands — OpenHands SDK on top of cyberarena/agent-base.
# Build: docker build -t cyberarena/agent-openhands dockerfiles/agent/openhands
ARG BASE_IMAGE=cyberarena/agent-base
FROM ${BASE_IMAGE}

# OpenHands SDK (PyPI packages baked with uv, which comes from agent-base). Baked into
# a dedicated venv at /opt/openhands-venv; OpenHandsAgentManager drives it via a small
# SDK driver (agents/openhands_assets/driver.py) and does NO network install at game
# start (the isolated agent net blocks PyPI). PINNED — keep in sync with
# OPENHANDS_SDK_VERSION in agents/openhands.py. LocalWorkspace (subprocess, no
# Docker-in-Docker) is the SDK default; the driver never selects DockerWorkspace.
RUN uv venv /opt/openhands-venv \
    && VIRTUAL_ENV=/opt/openhands-venv uv pip install \
        openhands-sdk==1.36.1 openhands-tools==1.36.1 \
    && /opt/openhands-venv/bin/python -c "import openhands.sdk, openhands.tools"
