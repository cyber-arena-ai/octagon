#!/bin/sh
# Agent container entrypoint.
#
# Tools curl / pip / npm / git all honor HTTP_PROXY / HTTPS_PROXY env vars
# natively. Two notable holdouts need help:
#
#   1) apt — does NOT honor proxy env vars. Without explicit apt.conf the
#      orchestrator's `_run_install_list` (orchestrator/orchestrator.py:67)
#      `apt-get update && apt-get install ...` tries direct egress and hangs
#      / fails on an internal network. Translate the env to apt.conf so the
#      first apt-get hit after container start already knows the proxy.
#
#   2) Node built-in fetch (undici) — does NOT honor proxy env unless
#      NODE_USE_ENV_PROXY=1 is exported. Claude Code talks to apirouter on
#      the SAME internal network (direct, NO_PROXY-ed), so this isn't
#      load-bearing for the core LLM path; we still flip it on so any
#      ad-hoc node script the agent writes routes correctly.
set -e

PROXY="${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY:-${http_proxy:-}}}}"

if [ -n "$PROXY" ]; then
    cat > /etc/apt/apt.conf.d/99proxy-from-env <<EOF
Acquire::http::Proxy  "$PROXY";
Acquire::https::Proxy "$PROXY";
EOF
fi

exec "$@"
