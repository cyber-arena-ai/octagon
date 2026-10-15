#!/usr/bin/env bash
# Dev-deps preflight: install supplementary tooling into a team's dev container
# (orchestrator step 6). Install-if-missing — the probe and the install list are
# derived from ONE sentinel->package map, so they can't drift, and only the
# genuinely-absent packages are fetched (a fully baked image is a no-op). The
# orchestrator runs this best-effort per team: a failure is logged and isolated
# to that team, never aborting the others.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# Single source of truth: sentinel binary -> apt package. Every package the
# script can install has a probe here, so "already provisioned?" reflects the
# whole set — not a hand-picked subset that silently misses tools.
declare -A APT_TOOLS=(
    [rg]=ripgrep [fdfind]=fd-find [jq]=jq [batcat]=bat
    [nc]=netcat-openbsd [ping]=iputils-ping [curl]=curl [wget]=wget
    [dig]=dnsutils [ip]=iproute2 [ifconfig]=net-tools [socat]=socat
    [xxd]=xxd [file]=file [objdump]=binutils [unzip]=unzip [7z]=p7zip-full
    [gcc]=build-essential [make]=build-essential [go]=golang-go
    [git]=git [sqlite3]=sqlite3 [pip3]=python3-pip
)

missing_pkgs=()
for bin in "${!APT_TOOLS[@]}"; do
    command -v "$bin" >/dev/null 2>&1 || missing_pkgs+=("${APT_TOOLS[$bin]}")
done

if ((${#missing_pkgs[@]})); then
    # Retry transient mirror/DNS blips instead of failing on the first one.
    apt-get -o Acquire::Retries=3 update
    apt-get install -y --no-install-recommends \
        $(printf '%s\n' "${missing_pkgs[@]}" | sort -u)
    apt-get clean && rm -rf /var/lib/apt/lists/*
fi

# python module -> pip package, same single-source-of-truth pattern: check each
# module independently so a partially-present set still gets topped up.
declare -A PIP_MODS=(
    [Crypto]=pycryptodome [cryptography]=cryptography [nacl]=pynacl
    [jwt]=pyjwt [requests]=requests
)

missing_pips=()
for mod in "${!PIP_MODS[@]}"; do
    python3 -c "import $mod" >/dev/null 2>&1 || missing_pips+=("${PIP_MODS[$mod]}")
done

if ((${#missing_pips[@]})); then
    python3 -m pip install --break-system-packages --no-cache-dir "${missing_pips[@]}"
fi

echo "dev-setup.sh: done"
