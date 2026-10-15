"""Per-run docker resource namespacing.

To let several game runs share one host, every HOST-GLOBAL docker identifier — a
container name, a network name, the compose project — is prefixed with a per-run
`namespace` (NS). The in-network DNS name stays the bare `node.id` (set as a
network alias), so agent-facing surfaces (env CYBERARENA_OWN_PROD, SSH hostnames,
http://api-router:8080, etc.) are unchanged and need no rewriting.

`qualify` is the single chokepoint. NS="" (the default) returns the name verbatim,
so a run without `--namespace` is byte-identical to the pre-namespacing behaviour.
"""

from __future__ import annotations


def qualify(name: str, ns: str = "") -> str:
    """Prefix a docker container/network/project name with the run namespace.
    NS="" → unchanged (back-compat)."""
    return f"{ns}-{name}" if ns else name
