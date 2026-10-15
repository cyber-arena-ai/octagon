# cyberarena/agent-deepseek — DeepSeek Harness (`dsh`) on top of cyberarena/agent-base.
# Serves both registry keys `deepseek` and `dsh` (same adapter).
# Build: docker build -t cyberarena/agent-deepseek dockerfiles/agent/deepseek
ARG BASE_IMAGE=cyberarena/agent-base
FROM ${BASE_IMAGE}

# DeepSeek Harness (`dsh`) CLI (official npm). Baked as the SOLE install path —
# DeepSeekAgentManager.install verifies this exact pin via verify_cli and does NO
# runtime package install (runtime installs are forbidden). The `dsh` binary and its
# bundled @deepseek-ai/dsh-mcp-client ship in the package. PINNED — keep in sync with
# DSH_VERSION in agents/deepseek.py (tests/test_deepseek_agent.py asserts this file
# names that version exactly once).
RUN npm install -g @deepseek-ai/dsh@0.1.0-rc.6
COPY install_dsh_rc6.sh /usr/local/bin/install-dsh-rc6
RUN chmod +x /usr/local/bin/install-dsh-rc6 && /usr/local/bin/install-dsh-rc6

# Drop the npm cache (dead weight; see the codex image for the offline-reinstall
# rationale). Done AFTER install-dsh-rc6, which populates the isolated prefix.
RUN npm cache clean --force && rm -rf /root/.npm

# The isolated prefix is the only dsh install path. Keep it ahead of the legacy
# global npm bin directory so no stale package can shadow the pinned closure.
ENV PATH=/opt/dsh-rc6/node_modules/.bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

RUN dsh --version
