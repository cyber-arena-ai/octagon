# cyberarena/agent-base — the COMMON layer under every per-harness agent image.
#
# Layout (dockerfiles/agent/):
#   base/Dockerfile        -> cyberarena/agent-base        ubuntu + pentest/dev tooling +
#                                                          Node + uv + proxy-aware entrypoint.
#                                                          NO agent CLI.
#   <provider>/Dockerfile  -> cyberarena/agent-<provider>  FROM agent-base + that ONE harness
#                                                          (claude, codex, opencode, gemini,
#                                                          qwen, kimi, grok, deepseek, nexau,
#                                                          openhands).
#
# A game.yaml dev node names the image matching its team's `agent.provider`
# (`cyberarena/agent-claude`, `cyberarena/agent-codex`, ...); `script` / `idle` teams
# run this base directly. Everything an AGENT can invoke — pentest tools, python libs,
# node, uv — lives HERE, so every harness sees an identical toolset and the only
# difference between two harness images is the harness itself. Add agent-facing
# tooling here, never in a harness Dockerfile.
#
# Build: bash scripts/build_images.sh          (base first, then each harness on top)
#    or: docker build -t cyberarena/agent-base dockerfiles/agent/base
#        docker build -t cyberarena/agent-<p>  dockerfiles/agent/<p>
# A change here is only picked up by a harness image once THAT image is rebuilt
# (main.py's stale-image check watches both Dockerfiles for exactly this reason).
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
# Global npm packages go to /usr (-> /usr/bin), on PATH for every agent shell.
# The image family is the ONLY install path for the agent CLIs: the orchestrator
# used to re-run `npm i -g` per team at game start, and no longer does (it only
# preflights what the harness image baked — see agents/_provisioning.py::verify_cli).
# Consequence: bumping a pinned version in agents/*.py requires REBUILDING that
# harness image (`bash scripts/build_images.sh`), or the preflight logs a
# stale-image warning.
ENV NPM_CONFIG_PREFIX=/usr
ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin
# Make Node's built-in fetch (undici) honor HTTP_PROXY/HTTPS_PROXY env, which
# it does NOT do by default. Curl/pip/npm/git/wget already honor them
# natively; apt does NOT — the entrypoint below handles that one.
ENV NODE_USE_ENV_PROXY=1

# System runtime + deploy/attack/debug tooling. xz-utils unpacks the Node
# tarball below; curl/ca-certificates fetch it.
#
# Everything after `strace` bakes the supplementary dev toolset that
# config/docker/dev-setup.sh would otherwise apt/pip-install at game start.
# On the isolated agent network that runtime install is blocked by the
# apirouter egress firewall (the Ubuntu/PyPI repos aren't allow-listed), so
# dev-setup failed rc=100 and every match ran with a degraded toolset. Baking
# it here makes the dev-setup preflight a genuine no-op with zero runtime
# network dependency. Keep this set in sync with the APT_TOOLS / PIP_MODS maps
# in config/docker/dev-setup.sh. (#64)
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip \
        curl ca-certificates xz-utils \
        rsync openssh-client \
        nmap netcat-openbsd sqlmap gobuster \
        vim tmux tcpdump strace \
        ripgrep fd-find bat jq \
        wget dnsutils iproute2 net-tools iputils-ping socat \
        binutils xxd file build-essential python3-dev golang-go \
        git unzip p7zip-full sqlite3 \
    && pip3 install --break-system-packages --no-cache-dir \
        websockets requests httpx pwntools \
        pycryptodome cryptography pynacl pyjwt \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Node.js — official prebuilt binary from nodejs.org (NOT NodeSource, NOT apt's
# stale Node 18, NOT a source build). SHA256-verified against nodejs.org. Pinned
# to the current LTS; bump NODE_VERSION to upgrade. Extracted into /usr/local.
# Every npm-distributed harness (claude/codex/opencode/gemini/qwen/grok/dsh)
# needs Node >=20 or >=22; this satisfies all of them.
ARG NODE_VERSION=24.16.0
RUN set -eux; \
    cd /tmp; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) node_arch=x64 ;; \
        arm64) node_arch=arm64 ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    tarball="node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"; \
    base="https://nodejs.org/dist/v${NODE_VERSION}"; \
    curl -fsSLO "${base}/${tarball}"; \
    curl -fsSL "${base}/SHASUMS256.txt" | grep " ${tarball}\$" | sha256sum -c -; \
    tar -xJf "${tarball}" -C /usr/local --strip-components=1 --no-same-owner; \
    rm -f "${tarball}"; \
    node --version; npm --version

# uv — Python package manager. Used by the git-only / venv-baked harness images
# (nexau, openhands) at BUILD time, and available to agents at runtime. Installed
# into /usr/local/bin so downstream `uv` RUN steps find it: the curl|sh installer
# defaults to /root/.local/bin, which is NOT on this image's PATH.
ENV UV_INSTALL_DIR=/usr/local/bin
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && uv --version

# Entrypoint translates HTTPS_PROXY env -> /etc/apt/apt.conf.d/99proxy-from-env
# at container start, so the orchestrator's runtime `apt-get install` (used by
# `_run_install_list`) routes through apirouter on the isolated network. apt
# does NOT honor HTTPS_PROXY natively; this is the only clean workaround.
COPY entrypoint.sh /usr/local/bin/agent-entrypoint
RUN chmod +x /usr/local/bin/agent-entrypoint

# Fail the build early if the shared runtime is incomplete. Each harness image
# re-checks its own CLI on top of this.
RUN node --version && npm --version && python3 --version && uv --version \
    && nmap --version > /dev/null && python3 -c "import pwn, requests, httpx"

ENTRYPOINT ["/usr/local/bin/agent-entrypoint"]
CMD ["sleep", "infinity"]
