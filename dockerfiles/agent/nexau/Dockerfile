# cyberarena/agent-nexau — NexAU CLI on top of cyberarena/agent-base.
# Build: docker build -t cyberarena/agent-nexau dockerfiles/agent/nexau
ARG BASE_IMAGE=cyberarena/agent-base
FROM ${BASE_IMAGE}

# NexAU CLI (git+uv; there is NO npm/PyPI package). Baked here at a pinned tag and
# exposed as `nexau` on PATH. Unlike the npm adapters, NexauAgentManager.install does
# NO network install — the isolated agent network blocks git/PyPI, so NexAU MUST be
# fully baked; install() only renders the per-team agent.yaml + tool descriptors +
# nexau.env. uv.lock is committed, so `uv sync --frozen` is reproducible. (`uv` and
# `git` come from agent-base.)
# PINNED — keep in sync with NEXAU_VERSION in agents/nexau.py.
# NEXAU_SHA pins the exact commit v0.3.9 pointed at when this was verified. A git TAG
# is mutable — whoever can push to nex-agi/NexAU could retarget v0.3.9, and that code
# runs as root in the agent container on a net that reaches the MCP gateway and prod.
# The rev-parse check fails the build loudly if the tag ever moves.
ARG NEXAU_SHA=35ee1861546db3cb280a6e17e38a74060d7c96c3
RUN git clone --depth 1 --branch v0.3.9 https://github.com/nex-agi/NexAU.git /opt/nexau \
    && cd /opt/nexau \
    && test "$(git rev-parse HEAD)" = "${NEXAU_SHA}" \
    && uv venv \
    && uv sync --frozen \
    && ln -s /opt/nexau/.venv/bin/nexau /usr/local/bin/nexau
# NexAU v0.3.9 bug fix: its MCP tool executor filters `agent_state` + `global_storage`
# out of the tool-call kwargs (their comment: "cause JSON serialization errors") but
# FORGETS `sandbox` — so calling ANY MCP tool while a LocalSandbox is attached fails
# with "Object of type LocalSandbox is not JSON serializable", which breaks submit_flag/
# report_ready/etc. Add `sandbox` to that filter. (Report upstream / drop when fixed.)
RUN sed -i 's/if k not in ("agent_state", "global_storage")/if k not in ("agent_state", "global_storage", "sandbox")/' \
        /opt/nexau/nexau/archs/tool/builtin/mcp_client.py \
    && grep -q '"agent_state", "global_storage", "sandbox"' /opt/nexau/nexau/archs/tool/builtin/mcp_client.py

RUN nexau --help > /dev/null
