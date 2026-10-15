"""Generate a per-run apirouter config from GameConfig.

Called by main.py before orch.setup(); the result is bind-mounted into
the api-router container so firewall rules and principals come from the
game config rather than the baked image config.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import secrets

import yaml

from gamemgr.config import GameConfig

log = logging.getLogger(__name__)


def routes_through_router(cfg: GameConfig, agent_cfg) -> bool:
    """Does this team's agent actually talk to the api-router?

    The api-router node id doubles as its docker hostname, so it appears in the
    base_url of every routed agent. A team pointed straight at a provider is NOT
    routed: it authenticates with its own real key, is never metered, and — the
    part that bites — must not be registered as a router principal, or that real
    key gets written into the generated config as a principal token.
    """
    if not cfg.api_router.container:
        return False
    return bool(agent_cfg.base_url) and cfg.api_router.container in agent_cfg.base_url


def _upstream_key_vars(cfg: GameConfig) -> dict[str, str]:
    """{upstream id: env var holding its real key}. The single-upstream shorthand
    has always used UPSTREAM_API_KEY; the multi form defaults to the
    API_ROUTER_KEY_<ID> convention (resolved in the game-config loader)."""
    ups = cfg.api_router.upstreams
    if not ups:
        return {"default": "UPSTREAM_API_KEY"}
    return {uid: u.api_key_env for uid, u in ups.items()}


def resolve_upstream_keys(cfg: GameConfig) -> list[str]:
    """Make sure every upstream's key actually reaches the api-router container,
    and fail loudly at startup if one can't.

    The router forwards with the upstream's key — a client's own credential is a
    principal token the router minted for billing, not a provider key, so there is
    no keyless mode to fall back to. An upstream whose key we cannot resolve is a
    dead run: the container would exit on startup while the game loop kept ticking,
    turning a one-line `.env` omission into a full match of AGENT_FAILED.

    A key reaches the container one of two ways, checked in this order:
      1. the api-router node already declares the var in `env:` (the pre-existing
         style — `UPSTREAM_API_KEY: ${API_ROUTER_KEY}`, expanded at config load);
      2. otherwise we inject it from the process env, which `load()` populated from
         `.env` — so the usual setup is just `API_ROUTER_KEY_<ID>=…` in `.env` and
         nothing in game.yaml at all.

    Mutates the router node's `env` in place. Returns the var names it injected.
    """
    if not cfg.api_router.container:
        return []
    router = next(
        (n for n in cfg.topology.nodes if n.id == cfg.api_router.container), None)
    if router is None:
        return []

    injected, missing = [], []
    for upstream_id, var in _upstream_key_vars(cfg).items():
        if (router.env.get(var) or "").strip():
            continue                                  # (1) supplied by the node
        value = (os.environ.get(var) or "").strip()   # (2) from .env / the process
        if not value:
            missing.append((upstream_id, var))
            continue
        router.env[var] = value
        injected.append(var)

    if missing:
        raise ValueError(
            "api-router: no API key for upstream(s) "
            + "; ".join(f"{uid!r} (needs {var})" for uid, var in missing)
            + f". Add {missing[0][1]}=<key> to .env, or declare it in "
            f"nodes[{router.id}].env. The router forwards with this key — a team's "
            "own token is a billing principal, not a provider credential, so there "
            "is nothing else it could send."
        )
    if injected:
        log.info("apirouter: resolved upstream key(s) %s from the environment",
                 ", ".join(injected))
    return injected


def assign_router_principals(cfg: GameConfig) -> dict[str, str]:
    """Resolve each router-routed team's principal token — the label its agent presents
    to the router and that the router attributes usage to.

    The token is a NON-SECRET per-team label (like `teams[].token` for flag
    submission), not a credential: the router substitutes the upstream's own key on
    forward (`resolve_upstream_keys` guarantees it has one), so this token never
    reaches a provider — it only names who to bill. So DECLARE it plainly in
    `agent.api_key`; that value is authoritative and is used verbatim. When a routed
    team leaves `agent.api_key` empty, a RANDOM `cad-<hex>` token is minted for it —
    random, not `cad-<team_id>`, so one adversarial agent can't guess and present a
    rival team's token to bill against it or drain its budget. Either way the value is
    copied into the agent's container env (so a team only ever sees its own) and into
    the router's principals map (whose `id` stays the readable team id).

    Tokens must be DISTINCT across routed teams — two teams on one token collapse into
    a single principal and attribution is lost — so a collision is rejected here rather
    than silently mis-billed.

    (`AgentConfig` is frozen, so a filled default is swapped in via `dataclasses.replace`;
    the `agents` dict is mutable and every reader looks the agent up by team id at launch.)

    Mutates `cfg.agents[*].api_key` for teams that had none. Returns {team_id: token}."""
    if not cfg.api_router.container:
        return {}
    router = next(
        (n for n in cfg.topology.nodes if n.id == cfg.api_router.container), None)
    if router is None:
        return {}

    assigned: dict[str, str] = {}
    for team_id, ac in list(cfg.agents.items()):
        if not routes_through_router(cfg, ac):
            continue
        tok = ac.api_key or f"cad-{secrets.token_hex(12)}"   # declared wins; else random
        if not ac.api_key:
            cfg.agents[team_id] = dataclasses.replace(ac, api_key=tok)
        assigned[team_id] = tok

    dupes = sorted({t for t in assigned.values() if list(assigned.values()).count(t) > 1})
    if dupes:
        raise ValueError(
            "router principal tokens must be distinct per team (a shared token collapses "
            f"two teams into one principal and loses attribution); duplicated: {dupes}. "
            "Give each team a distinct agent.api_key.")
    if assigned:
        log.info("apirouter: per-team principals %s", sorted(assigned))
    return assigned


def _warn_unenforceable_budgets(cfg: GameConfig, pricing: dict[str, dict[str, float]]) -> None:
    """A budget is enforced against cost, and cost comes from `pricing`. Cap a team
    whose model is priced at 0.0 and the cap can never be reached — spend stays $0
    however many tokens burn. Silent, and exactly the kind of thing you only notice
    from the bill, so say it out loud at startup."""
    for team_id, cap in cfg.api_router.budgets.items():
        agent_cfg = cfg.agents.get(team_id)
        if agent_cfg is None:
            continue
        rates = pricing.get(agent_cfg.model) or {}
        if rates.get("input", 0.0) <= 0.0 and rates.get("output", 0.0) <= 0.0:
            log.warning(
                "apirouter: team %s has budget_usd=%s but model %r is priced at 0 — "
                "spend will always be $0 and the cap can NEVER trigger. Set "
                "api_router.pricing.%s.{input,output} (USD per 1M tokens).",
                team_id, cap, agent_cfg.model, agent_cfg.model)


def _upstream_section(cfg: GameConfig) -> dict:
    """The `upstream:` (single) or `upstreams:` + `routes:` (multi) half of the
    router config — apirouter rejects the two spellings together, so emit exactly
    one. Keys stay as literal '${VAR}' placeholders for apirouter to expand from
    the container env at its own startup; the real key never lands on disk."""
    ar = cfg.api_router
    if not ar.upstreams:
        return {"upstream": {
            "base_url": ar.upstream_url or "https://app-us.ppapi.ai",
            "api_key": "${UPSTREAM_API_KEY}",
        }}
    section: dict = {
        "upstreams": {
            upstream_id: {"base_url": u.base_url, "api_key": "${%s}" % u.api_key_env}
            for upstream_id, u in ar.upstreams.items()
        },
        "routes": {"default": ar.route_default},
    }
    if ar.route_by_model:
        section["routes"]["by_model"] = dict(ar.route_by_model)
    return section


def generate(cfg: GameConfig) -> str:
    """Return a complete apirouter config YAML for this run.

    Principals are derived from each team's agent api_key (already env-expanded
    by the game config loader). Firewall rules come from cfg.api_router.firewall.
    Every upstream api_key is kept as a literal '${VAR}' placeholder so the real
    key is expanded from the container env and never written to disk.
    Everything else uses the same defaults as dockerfiles/api-router-cfg/config.yaml.
    """
    fw = cfg.api_router.firewall

    # ONLY teams that actually route through the router (see routes_through_router).
    # A team pointed straight at a provider holds its own REAL key; registering it as
    # a principal would both be meaningless (it never presents a token to the router)
    # and write that real key, verbatim, into a run artifact meant for consumers.
    principals = []
    for team_id, agent_cfg in cfg.agents.items():
        if not (agent_cfg.api_key and routes_through_router(cfg, agent_cfg)):
            continue
        row = {"id": team_id, "token": agent_cfg.api_key}
        cap = cfg.api_router.budgets.get(team_id)
        if cap is not None:
            row["budget_usd"] = cap
        principals.append(row)

    skipped = [t for t, a in cfg.agents.items() if not routes_through_router(cfg, a)]
    if skipped:
        log.warning(
            "apirouter: team(s) %s bypass the router (agent.base_url points straight "
            "at a provider) — they use their own api_key, and their LLM spend is NOT "
            "metered or firewalled", ", ".join(sorted(skipped)))

    # Every declared model gets a row so its usage is always attributable; rates come
    # from game.yaml and default to 0.0 = free (tokens still counted, cost reported as
    # zero), which is the behaviour every pre-pricing config had. A model's game.yaml
    # entry may add `cache_read`/`cache_write` on top of `input`/`output` — those pass
    # through verbatim via the .update() below; `cost_from_pricing` already defaults a
    # missing cache rate to 0.0, so an unpriced model here stays exactly as free as before.
    models = sorted({ac.model for ac in cfg.agents.values() if ac.model})
    pricing = {m: {"input": 0.0, "output": 0.0} for m in models}
    pricing.update({m: dict(r) for m, r in cfg.api_router.pricing.items()})
    _warn_unenforceable_budgets(cfg, pricing)

    data = {
        "listen": {"gateway": "0.0.0.0:8080", "proxy": "0.0.0.0:3128"},
        **_upstream_section(cfg),
        "principals": principals,
        "pricing": pricing,
        "compat": {
            "drop_empty_content": True,
            "drop_params": ["reasoning_effort"],
            "json_schema_fallback": True,
            "retry_on_5xx": 2,
            "retry_backoff_ms": 500,
            "max_request_bytes": 5242880,
        },
        "firewall": {
            "default": fw.default,
            "allow": {
                "domains": list(fw.allow_domains),
                "cidrs": list(fw.allow_cidrs),
            },
            "deny": {
                "domains": list(fw.deny_domains),
                "cidrs": list(fw.deny_cidrs),
            },
        },
        "metering": {"jsonl_path": "usage.jsonl", "stdout": True},
    }
    return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
