# cyberarena/agent-kimi — Kimi Code CLI on top of cyberarena/agent-base.
# Build: docker build -t cyberarena/agent-kimi dockerfiles/agent/kimi
ARG BASE_IMAGE=cyberarena/agent-base
FROM ${BASE_IMAGE}

# Kimi Code CLI. Python-native and baked as the sole install path; the manager
# verifies this exact pin at game startup and performs no runtime package install.
# Keep in sync with KIMI_VERSION in agents/kimi.py.
RUN pip3 install --break-system-packages --no-cache-dir kimi-cli==1.49.0

RUN kimi --version
