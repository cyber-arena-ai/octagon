"""Egress firewall rule engine — pure, no I/O.

A ``Firewall`` decides whether an outbound request to a given host (and,
optionally, resolved IP) is permitted. Rules are firewall-style:

  * deny list wins (a host matching any deny rule is rejected outright),
  * then allow list (a host matching any allow rule is permitted),
  * else the configured default (``allow`` or ``deny``).

Domains match via ``fnmatch`` globs (case-insensitive), so ``*.ppapi.ai``
matches ``app-us.ppapi.ai``. IPs match via CIDR membership.

This is consumed by the forward proxy (proxy.py) for both plain-HTTP
absolute-form requests and HTTPS ``CONNECT`` tunnels (filtered by the CONNECT
host — no TLS interception).
"""

from __future__ import annotations

import fnmatch
import ipaddress
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


@dataclass
class Firewall:
    # "allow" or "deny" — applied when no allow/deny rule matches.
    default: str = "deny"
    allow_domains: list[str] = field(default_factory=list)
    allow_cidrs: list[str] = field(default_factory=list)
    deny_domains: list[str] = field(default_factory=list)
    deny_cidrs: list[str] = field(default_factory=list)

    def _domain_match(self, host: str, patterns: list[str]) -> str | None:
        h = host.casefold()
        for p in patterns:
            if fnmatch.fnmatch(h, p.casefold()):
                return p
        return None

    def _cidr_match(self, ip: str, cidrs: list[str]) -> str | None:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        for c in cidrs:
            try:
                if addr in ipaddress.ip_network(c, strict=False):
                    return c
            except ValueError:
                continue
        return None

    def decide(self, host: str, ip: str | None = None) -> Decision:
        """Decide whether egress to ``host`` (optionally with resolved ``ip``)
        is allowed. ``host`` should be a bare hostname or IP literal (no port)."""
        host = (host or "").strip()

        d = self._domain_match(host, self.deny_domains)
        if d is not None:
            return Decision(False, f"deny:domain:{d}")
        if ip is not None:
            d = self._cidr_match(ip, self.deny_cidrs)
            if d is not None:
                return Decision(False, f"deny:cidr:{d}")

        a = self._domain_match(host, self.allow_domains)
        if a is not None:
            return Decision(True, f"allow:domain:{a}")
        if ip is not None:
            a = self._cidr_match(ip, self.allow_cidrs)
            if a is not None:
                return Decision(True, f"allow:cidr:{a}")

        allowed = self.default.strip().lower() == "allow"
        return Decision(allowed, f"default:{self.default}")
