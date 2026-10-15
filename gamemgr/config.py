"""Loader for the full game.yaml — topology + challenges + round config + agents.

Also resolves `${VAR}` placeholders against the process environment (after
loading .env) so secrets never live in the committed yaml.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml
from dotenv import load_dotenv

from models.agent import AgentConfig
from models.topology import Network, Node, Team, TopologyGraph

log = logging.getLogger(__name__)

# The only keys honored inside a team's `agent:` block. Every other
# AgentConfig field (team_id, mcp_endpoint, submit_url, …) is filled in by
# the driver, never read from game.yaml — so an unrecognized key here is
# always a typo or a false expectation, and is rejected (see
# `_validate_agent_block`) instead of being silently dropped by `ag.get`.
_ALLOWED_AGENT_KEYS = frozenset(
    {"cli", "provider", "model", "api_key", "base_url", "append_system_prompt",
     "security_scanner"}
)


@dataclass(frozen=True)
class ChallengeConfig:
    name: str
    type: str
    port: int
    deploy_on: list[str]    # team ids
    health_interval_secs: float | None = None
    """Per-match override of the challenge's intrinsic poll interval.

    Precedence: this (game.yaml) > `Challenge.health_interval_secs` >
    `health.poller.DEFAULT_INTERVAL_SECS`."""
    level: str = "easy"
    """Difficulty level for this challenge in this match: "easy" | "hard".

    Drives the agent briefing's ending: "easy" (default) includes the
    challenge's class-level `Hint:` line; "hard" replaces it with the fixed
    no-hint sentence, so the agent must find the vulnerability unaided. A future
    difficulty-grading effort builds on this."""
    source_in_dev: bool = False
    """Stage the vulnerable-service source in the dev (agent) container.

    Default False preserves the prod-only source and existing prompt in normal
    async/sync runs. Solo-attack YAML configs default this on at load time unless
    the challenge explicitly sets `source_in_dev: false`. When True,
    provisioning copies prod's materialized source to
    /srv/challenge/<challenge-name>/ in dev and the prompt names that path.

    CONTRACT: enabling this hands the agent a copy of prod's whole
    /srv/challenge tree. Secret filtering is a fixed basename denylist
    (flag, flag.txt, readflag, checker.sh, restart.sh, scoring.sh, score.sh,
    entrypoint.sh; plus .git/__pycache__), so ANY differently-named secret in
    that tree — a flag under another name, a flag baked into a source/config
    file, .env, private keys — WOULD be staged to the agent. A challenge may set
    this True ONLY if its prod /srv/challenge holds no secret beyond those
    excluded files."""


@dataclass(frozen=True)
class RoundConfig:
    defense_secs: int = 30
    attack_secs: int = 30
    total_rounds: int = 3
    # "sync"  — strict DEFENSE→ATTACK phases, fresh agent process per phase.
    # "async" — one long-lived agent per team (prompted once, supervised
    #           respawn via --continue); networks always connected; a round
    #           tick just rotates flags every `round_secs`.
    # "solo"  — one real LLM agent in a 2-team async game (see `solo_role`).
    #           Same driver + lifecycle as async (one long-lived supervised agent
    #           per team, LIVE the whole game, round tick rotates flags). The solo
    #           agent (team1 by config convention) gets a role-specific defense- or
    #           attack-only briefing; its counterpart is an automatic non-LLM team
    #           (a `script` verifier for defense, an `idle` target for attack).
    mode: str = "sync"
    # Async/solo only: seconds between flag rotations (one "round" tick).
    # Ignored in sync mode (which uses defense_secs / attack_secs).
    round_secs: int = 60
    # Solo only: the ROLE of the solo agent (the real LLM). "defense" hardens its
    # own service (counterpart = a `script` verifier that runs the ground-truth
    # exploit); "attack" exploits a static target (counterpart = an `idle` agent).
    # Required when mode == "solo"; empty/ignored otherwise. Validated in `load()`.
    solo_role: str = ""

    @property
    def is_solo(self) -> bool:
        """One real LLM agent in a 2-team async game (see `solo_role`)."""
        return self.mode == "solo"

    @property
    def is_solo_defense(self) -> bool:
        """Solo run whose real agent DEFENDS (counterpart = script verifier). The
        verification machinery (verifier endpoint/tracker/result) is gated on this."""
        return self.mode == "solo" and self.solo_role == "defense"

    @property
    def is_solo_attack(self) -> bool:
        """Solo run whose real agent ATTACKS (counterpart = idle target). Success
        is the stock capture signal; no verifier machinery."""
        return self.mode == "solo" and self.solo_role == "attack"

    @property
    def uses_async_driver(self) -> bool:
        """Modes driven by `AsyncLoop` (one long-lived supervised agent, LIVE the
        whole game). Both async and solo run it — this is the check the loop /
        supervisor selection uses, so neither string-compares `mode` directly."""
        return self.mode in ("async", "solo")


@dataclass(frozen=True)
class FlagServiceConfig:
    """The containerized flag-service (v3). A SINGLE service, off the agent net,
    holds all teams' flags + the full token map. GameMgr configures it, signals
    it to plant over SSH each round, and FORWARDS every submit to it; it records
    FLAG_SUBMITTED via its EventSink. `base_url` = its host-reachable published
    port; `container` = its node id (for SSH-key provisioning). Required by v3
    (no in-process flag fallback)."""
    base_url: str = ""
    container: str = ""


@dataclass(frozen=True)
class HealthPollerConfig:
    """The containerized health-poller (v3). GameMgr drives its control endpoints
    via `RemoteHealthPollerHandle` at `base_url`; the container schedules probes
    and runs each by SSHing into prod. `container` = its node id (for SSH-key
    provisioning).

    `recent_results_default` is the number of past health-check runs the
    `get_health_checks` MCP tool returns when the agent doesn't pass an
    explicit limit (it returns fewer if the timeline holds fewer)."""
    base_url: str = ""
    container: str = ""
    recent_results_default: int = 10


@dataclass(frozen=True)
class MCPGatewayConfig:
    """The containerized MCP server (v3). Agents' `mcp_endpoint` points at the
    on-net mcp-gateway (`agent_url` + the MCP path) — no api-router relay.
    `agent_url` is the gateway's address ON the agent network
    (e.g. http://mcp-gateway:8080)."""
    agent_url: str = ""


@dataclass(frozen=True)
class ApiRouterFirewallConfig:
    default: str = "deny"
    allow_domains: list[str] = field(default_factory=list)
    allow_cidrs: list[str] = field(default_factory=list)
    deny_domains: list[str] = field(default_factory=list)
    deny_cidrs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ApiRouterUpstreamConfig:
    """One LLM provider behind the router.

    `api_key_env` names the env var holding this provider's real key. It is a bare
    name, not a `${...}` ref, so the game-config loader does not expand it — the
    generated router config carries only the placeholder and the value is resolved
    inside the api-router container, keeping the key off disk.

    It defaults by convention to `API_ROUTER_KEY_<UPSTREAM_ID>` (upper-cased), so the
    usual setup is: put the key in `.env` under that name and declare nothing else.
    Set it explicitly only to point at an env var you already have."""
    base_url: str
    api_key_env: str


@dataclass(frozen=True)
class ApiRouterConfig:
    """Per-run api-router settings read from game.yaml.

    At startup, main.py generates a full apirouter config YAML (principals
    derived from team agents, firewall from this config) and bind-mounts it
    into the `container` node so the game config — not the baked image config —
    controls which domains agents can reach.

    Upstreams come in two spellings, never both: `upstream_url` is the
    single-provider shorthand, `upstreams` + `routes` fronts several providers on
    separate keys (e.g. claude-* on the official Anthropic key, the rest via a
    reseller). Requests route on the model name the agent sends."""
    container: str = ""
    upstream_url: str = ""
    upstreams: dict[str, ApiRouterUpstreamConfig] = field(default_factory=dict)
    route_default: str = ""
    route_by_model: tuple[tuple[str, str], ...] = ()   # ordered (model glob, upstream id)
    firewall: ApiRouterFirewallConfig = field(default_factory=ApiRouterFirewallConfig)
    # USD per 1M tokens, keyed by the model name the agent sends:
    # {model: {input, output, cache_read, cache_write}}. `cache_read`/`cache_write`
    # are optional and default to 0.0 — an {input, output}-only entry is still
    # valid, it just leaves cache tokens unbilled. Absent model ⇒ 0.0 across the
    # board, i.e. free — so leaving this out keeps the pre-existing behaviour
    # (tokens counted, cost reported as zero).
    pricing: dict[str, dict[str, float]] = field(default_factory=dict)
    # Per-team USD spend cap, {team_id: usd}. Absent team ⇒ unlimited. The router
    # rejects a team's further requests with 402 once it reaches the cap.
    #
    # Enforced against the cost computed from `pricing` — so a budget on a model with
    # no price can never fire (spend stays 0.0 forever). `apirouter_cfg.generate`
    # warns when a config asks for that.
    budgets: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class VerifierConfig:
    """Solo DEFENSE only: paces the `script` verifier team's exploit loop and
    defines when a patch counts as verified (see docs/archived/solo_mode_plan.md
    §4b/§4c). Provisioned into the verifier container's env so `verify_runner.py`
    self-paces; also read host-side for the readiness gate + the verified-fix
    latch. Meaningless for attack solo / async — a `verifier:` block outside
    solo-defense is a ConfigError (validated in `load()`)."""
    required: bool = True                 # fail loud at bring-up if no runnable exploit
    initial_delay_seconds: int = 10       # wait before the first attempt (let bring-up settle)
    interval_seconds: int = 30            # gap between attempts (deliberately unhurried)
    timeout_seconds: int = 20             # per-attempt hard timeout on the exploit subprocess
    min_blocked_after_restart: int = 2    # N consecutive post-epoch BLOCKED ⇒ verified_fixed


@dataclass(frozen=True)
class GameConfig:
    topology: TopologyGraph
    challenges: list[ChallengeConfig] = field(default_factory=list)
    rounds: RoundConfig = field(default_factory=RoundConfig)
    agents: dict[str, AgentConfig] = field(default_factory=dict)  # team_id -> AgentConfig
    flag_service: FlagServiceConfig = field(default_factory=FlagServiceConfig)
    health_poller: HealthPollerConfig = field(default_factory=HealthPollerConfig)
    mcp_gateway: MCPGatewayConfig = field(default_factory=MCPGatewayConfig)
    api_router: ApiRouterConfig = field(default_factory=ApiRouterConfig)
    # Solo-defense only: verifier cadence + verified-fix threshold (see
    # VerifierConfig). Defaults are inert unless mode=solo/solo_role=defense.
    verifier: VerifierConfig = field(default_factory=VerifierConfig)
    # Opt-in (default off): at end of run, `docker commit` each team's patched
    # prod container and `docker save` it into the run dir (see
    # orchestrator.vulbox_snapshot). Off by default because the tar is large (a
    # full challenge image). Mode-agnostic, but the intended use is solo runs
    # (hand the hardened box to a human for offline verification).
    snapshot_patched_vulbox: bool = False


_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
_MAX_EXPAND_DEPTH = 8


def _validate_team_count(n_teams: int, mode: str) -> None:
    """Team-count is a function of mode: every mode here is a 2-team topology.
    sync/async are adversarial (need an opponent); solo is one real LLM agent +
    one automatic non-LLM counterpart (a `script` verifier or `idle` target),
    which is exactly 2 teams. A mismatch is a config mistake — fail loud.

    Enforced at the loader boundary (`load`), NOT in `_from_dict`, so unit tests
    that build partial configs directly (single- or zero-team fixtures for
    focused wiring tests) are unaffected; only a real `game.yaml` load enforces
    it."""
    if mode == "solo" and n_teams != 2:
        raise ValueError(
            f"round_config.mode 'solo' takes exactly two teams (the real agent "
            f"+ its automatic counterpart), got {n_teams}"
        )
    if mode in {"sync", "async"} and n_teams < 2:
        raise ValueError(
            f"round_config.mode {mode!r} needs at least two teams (there is no "
            f"opponent with one), got {n_teams}"
        )


def _provider_key(cfg: AgentConfig) -> str:
    """The classification key for a team's agent: the explicit `provider`, else
    the first token of `cli`. Mirrors `agents.registry.resolve_cls` key selection.
    Anything other than 'script' / 'idle' is a real-LLM agent (incl. the default
    claude fallback for a bare test cli)."""
    key = (cfg.provider or "").strip()
    if not key and cfg.cli:
        key = cfg.cli.split(None, 1)[0]
    return key


def _validate_solo(cfg: GameConfig, has_verifier_block: bool) -> None:
    """Validate a solo config's role topology (needs the resolved agents/providers,
    so it runs in `load()`, not `_validate_team_count`). `solo_role` names the role
    of the real agent; its counterpart's provider is implied and must be present:
      - defense → one real-LLM defender + one `script` verifier;
      - attack  → one real-LLM attacker + one `idle` target.
    Roles are resolved BY PROVIDER, never by team name (team1/team2 is only an
    example-config convention)."""
    role = cfg.rounds.solo_role
    if role not in {"defense", "attack"}:
        raise ValueError(
            "round_config.mode 'solo' requires round_config.solo_role to be "
            f"'defense' or 'attack', got {role!r}"
        )
    # Both teams must have an agent — the counterpart is an explicit non-LLM agent
    # (script/idle), never agent-less, so the 2-team + 2-agent topology is uniform.
    missing = [t.id for t in cfg.topology.teams if t.id not in cfg.agents]
    if missing:
        raise ValueError(
            f"solo mode needs an agent on every team; team(s) {missing} have none "
            "(the counterpart must be an explicit 'script' verifier or 'idle' target)"
        )
    keys = sorted(_provider_key(cfg.agents[t.id]) for t in cfg.topology.teams)
    counterpart = "script" if role == "defense" else "idle"
    forbidden = "idle" if role == "defense" else "script"
    n_counterpart = sum(1 for k in keys if k == counterpart)
    n_real = sum(1 for k in keys if k not in {"script", "idle"})
    if n_counterpart != 1 or n_real != 1 or any(k == forbidden for k in keys):
        raise ValueError(
            f"solo_role {role!r} needs exactly one real-LLM {role}r and one "
            f"'{counterpart}' counterpart; got providers {keys}"
        )
    if has_verifier_block and not cfg.rounds.is_solo_defense:
        raise ValueError(
            "a `verifier:` block is only valid for round_config.solo_role 'defense'; "
            f"got solo_role {role!r}"
        )


def _apply_solo_attack_source_defaults(raw: dict, cfg: GameConfig) -> GameConfig:
    """Default solo-attack runs to source_in_dev unless explicitly opted out.

    The low-level ChallengeConfig default stays False for async/sync and for
    tests that construct configs directly. At the real YAML boundary, solo
    attack is a source-study benchmark against a static target; staging the
    materialized vulnerable source into the dev box removes SSH/path-discovery
    overhead while keeping a per-challenge escape hatch for unsafe source trees.
    """
    if not cfg.rounds.is_solo_attack:
        return cfg
    raw_challenges = raw.get("challenges", []) if isinstance(raw, dict) else []
    if not cfg.challenges or not isinstance(raw_challenges, list):
        return cfg
    updated: list[ChallengeConfig] = []
    changed = False
    for index, challenge in enumerate(cfg.challenges):
        raw_challenge = raw_challenges[index] if index < len(raw_challenges) else {}
        if isinstance(raw_challenge, dict) and "source_in_dev" in raw_challenge:
            updated.append(challenge)
            continue
        updated.append(replace(challenge, source_in_dev=True))
        changed = True
    if not changed:
        return cfg
    return replace(cfg, challenges=updated)


def load(path: str | Path, *, env_file: str | Path | None = ".env") -> GameConfig:
    if env_file is not None and Path(env_file).is_file():
        load_dotenv(env_file, override=False)
    with open(path) as f:
        raw = yaml.safe_load(f)
    raw = _expand_env_vars(raw)
    cfg = _from_dict(raw)
    _validate_team_count(len(cfg.topology.teams), cfg.rounds.mode)
    if cfg.rounds.is_solo:
        _validate_solo(cfg, has_verifier_block="verifier" in (raw or {}))
        cfg = _apply_solo_attack_source_defaults(raw or {}, cfg)
    elif "verifier" in (raw or {}):
        raise ValueError(
            "a `verifier:` block is only valid in solo mode (solo_role: defense)"
        )
    return cfg


def _expand_env_vars(node):
    """Walk a parsed-yaml tree and substitute ${VAR} on every string value.

    Recursion is over the *tree structure* (dict/list/str), not over the
    substituted text. To still support env values that themselves contain
    `${...}` we re-run the substitution on each string in a bounded loop
    with a visited set to detect cycles.
    """
    if isinstance(node, dict):
        return {k: _expand_env_vars(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_env_vars(v) for v in node]
    if isinstance(node, str):
        return _expand_str(node)
    return node


def _expand_str(s: str) -> str:
    visited: set[str] = set()
    for _ in range(_MAX_EXPAND_DEPTH):
        matches = _VAR_RE.findall(s)
        if not matches:
            return s
        for var in matches:
            if var in visited:
                raise ValueError(
                    f"cyclic env var reference involving ${{{var}}} while expanding {s!r}"
                )
            if var not in os.environ:
                raise ValueError(f"env var {var} referenced in game.yaml but not set")
        visited.update(matches)
        s = _VAR_RE.sub(lambda m: os.environ[m.group(1)], s)
    raise ValueError(f"env var expansion exceeded depth {_MAX_EXPAND_DEPTH} on {s!r}")


def _challenge_from_dict(index: int, c: dict) -> ChallengeConfig:
    interval = c.get("health_interval_secs")
    if interval is not None:
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or interval <= 0
        ):
            raise ValueError(
                f"challenges[{index}] {c.get('name')!r}: health_interval_secs "
                f"must be a positive number, got {interval!r}"
            )
        interval = float(interval)
    level = c.get("level", "easy")
    if level not in ("easy", "hard"):
        raise ValueError(
            f"challenges[{index}] {c.get('name')!r}: level must be 'easy' or "
            f"'hard', got {level!r}"
        )
    source_in_dev = c.get("source_in_dev", False)
    if not isinstance(source_in_dev, bool):
        raise ValueError(
            f"challenges[{index}] {c.get('name')!r}: source_in_dev must be "
            f"a boolean, got {source_in_dev!r}"
        )
    return ChallengeConfig(
        name=c["name"],
        type=c["type"],
        port=int(c["port"]),
        deploy_on=list(c["deploy_on"]),
        health_interval_secs=interval,
        level=level,
        source_in_dev=source_in_dev,
    )


def _load_router_upstreams(
    ar_raw: dict,
) -> tuple[dict[str, ApiRouterUpstreamConfig], str, tuple[tuple[str, str], ...]]:
    """Parse api_router.upstreams / .routes, rejecting the ambiguous combination.

    `upstream_url` is the one-provider shorthand and cannot be mixed with the
    multi-provider form: a config where "which key paid for this call?" depends on
    a precedence rule between two blocks is one that will mislead its next editor,
    so we refuse it at load rather than pick a winner.
    """
    upstreams_raw = ar_raw.get("upstreams") or {}
    routes_raw = ar_raw.get("routes") or {}
    if not upstreams_raw and not routes_raw:
        return {}, "", ()

    if ar_raw.get("upstream_url"):
        raise ValueError(
            "api_router.upstream_url (single-upstream shorthand) cannot be combined "
            "with api_router.upstreams / api_router.routes. Drop upstream_url, list "
            "every provider under `upstreams:`, and name the fallback in "
            "`routes.default`."
        )
    if not upstreams_raw:
        raise ValueError("api_router.routes requires api_router.upstreams")

    upstreams: dict[str, ApiRouterUpstreamConfig] = {}
    for upstream_id, spec in upstreams_raw.items():
        spec = spec or {}
        if not spec.get("base_url"):
            raise ValueError(f"api_router.upstreams.{upstream_id}.base_url is required")
        upstreams[str(upstream_id)] = ApiRouterUpstreamConfig(
            base_url=str(spec["base_url"]),
            api_key_env=str(spec.get("api_key_env") or _default_key_env(str(upstream_id))),
        )

    route_default = str(routes_raw.get("default", "") or "")
    if not route_default:
        if len(upstreams) > 1:
            raise ValueError(
                "api_router.routes.default is required when more than one upstream is "
                f"declared (upstreams: {sorted(upstreams)}) — it is where a model that "
                "matches no by_model rule goes."
            )
        route_default = next(iter(upstreams))

    by_model = tuple(
        (str(pattern), str(upstream_id))
        for pattern, upstream_id in (routes_raw.get("by_model") or {}).items()
    )
    for upstream_id in (route_default, *(u for _, u in by_model)):
        if upstream_id not in upstreams:
            raise ValueError(
                f"api_router.routes points at unknown upstream {upstream_id!r}; "
                f"declared upstreams: {sorted(upstreams)}"
            )
    return upstreams, route_default, by_model


def _default_key_env(upstream_id: str) -> str:
    """The `.env` var an upstream's key is read from, by convention.

    Upstream ids are free-form (`my-vllm`, `openai.eu`), but env var names are not:
    anything outside [A-Z0-9_] cannot be exported from a shell, so a naive upper()
    would derive `API_ROUTER_KEY_MY-VLLM` — a name nobody can actually set. Punctuation
    folds to `_`.
    """
    safe = re.sub(r"[^A-Za-z0-9]", "_", upstream_id).upper()
    return f"API_ROUTER_KEY_{safe}"


def _load_router_pricing(ar_raw: dict) -> dict[str, dict[str, float]]:
    """`api_router.pricing`: {model: {input, output, cache_read, cache_write}} in
    USD per 1M tokens. `cache_read`/`cache_write` are optional — a model priced
    with only `input`/`output` is valid and simply leaves cache tokens unbilled
    (matching pre-cache-pricing behaviour)."""
    pricing: dict[str, dict[str, float]] = {}
    for model, rates in (ar_raw.get("pricing") or {}).items():
        rates = rates or {}
        row: dict[str, float] = {}
        for side in ("input", "output"):
            value = rates.get(side, 0.0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(
                    f"api_router.pricing.{model}.{side} must be a non-negative number "
                    f"(USD per 1M tokens), got {value!r}")
            row[side] = float(value)
        for side in ("cache_read", "cache_write"):
            if side not in rates:
                continue
            value = rates[side]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(
                    f"api_router.pricing.{model}.{side} must be a non-negative number "
                    f"(USD per 1M tokens), got {value!r}")
            row[side] = float(value)
        pricing[str(model)] = row
    return pricing


def _load_router_budgets(ar_raw: dict, team_ids: set[str]) -> dict[str, float]:
    """`api_router.budgets`: {team_id: usd}. Keyed by team because a router principal
    IS a team. A budget naming an unknown team is a typo that would otherwise cap
    nothing at all, so — like a route to an undeclared upstream — it is rejected."""
    budgets: dict[str, float] = {}
    for team_id, cap in (ar_raw.get("budgets") or {}).items():
        team_id = str(team_id)
        if team_id not in team_ids:
            raise ValueError(
                f"api_router.budgets names unknown team {team_id!r}; "
                f"declared teams: {sorted(team_ids)}")
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap < 0:
            raise ValueError(
                f"api_router.budgets.{team_id} must be a non-negative number (USD), "
                f"got {cap!r}")
        budgets[team_id] = float(cap)
    return budgets


def _validate_agent_block(team_id: str, ag: dict) -> None:
    """Reject an agent block with a missing `cli` or an unrecognized key.

    Without this, `ag.get(key, default)` silently drops a misspelled key
    (e.g. `append_sytem_prompt`), so the operator's intent is lost with no
    error. Fail loud so typos and false expectations surface at load."""
    if not isinstance(ag, dict):
        raise ValueError(
            f"team {team_id!r}: agent must be a mapping, got {type(ag).__name__}"
        )
    if "cli" not in ag:
        raise ValueError(f"team {team_id!r}: agent.cli is required")
    unknown = sorted(set(ag) - _ALLOWED_AGENT_KEYS)
    if unknown:
        raise ValueError(
            f"team {team_id!r}: unknown agent key(s) {unknown}; "
            f"allowed keys are {sorted(_ALLOWED_AGENT_KEYS)}"
        )


def _warn_ignored_cli_flags(team_id: str, cfg: AgentConfig) -> None:
    """Warn (don't fail) when `agent.cli` carries tokens the adapter ignores.

    `agent.cli` only selects the adapter (its leading token, unless
    `agent.provider` is set) and gates the npm install; the command that
    actually runs is fixed in the adapter's `phase_command`, so extra flags
    are otherwise a silent no-op. Skipped when:
      - the provider / cli leading token doesn't name a registered adapter,
        i.e. it would only reach a manager by DEFAULT fallback (e.g. the
        `cli: sleep 60` test stand-in) — its tokens aren't "ignored adapter
        flags", so warning about them would mislead; or
      - the resolved adapter honors the full cli verbatim
        (`CANONICAL_CLI is None`, e.g. the script adapter)."""
    from agents.registry import REGISTRY

    # Mirror agents.registry.resolve_cls's key resolution, but treat a
    # non-registered key as "no match" instead of silently falling back.
    key = (cfg.provider or "").strip()
    if not key and cfg.cli:
        key = cfg.cli.split(None, 1)[0]
    cls = REGISTRY.get(key)
    if cls is None:
        return
    canonical = cls.CANONICAL_CLI
    if canonical is None or cfg.cli.strip() == canonical:
        return
    log.warning(
        "team %s: agent.cli=%r carries flags beyond %r; the command that runs "
        "is fixed in the adapter's phase_command, so those extra flags have NO "
        "effect. Set agent.cli to %r to match what runs, or change flags in "
        "phase_command.",
        team_id, cfg.cli, canonical, canonical,
    )


# The pre-split all-in-one agent image. Named here only to give its former users
# a pointed migration hint instead of a generic "missing image" failure.
_RETIRED_AGENT_IMAGE = "cyberarena/agent"


def _warn_dev_image_mismatch(
    team_id: str, dev: str, image: str | None, cfg: AgentConfig,
) -> None:
    """Warn (don't fail) when a team's dev node image is not the one its agent
    provider is baked into.

    The agent images are per-harness (`cyberarena/agent-<provider>` on top of
    `cyberarena/agent-base`), so a claude team on the codex image has no `claude`
    binary and `install()` dies at `verify_cli`. A warning rather than an error:
    an operator may legitimately run a custom image derived from the expected one.
    Skipped when the dev node is undeclared (topology validation reports that)."""
    from agents.registry import expected_image

    if image is None:
        return
    expected = expected_image(cfg)
    if image == expected:
        return
    if image == _RETIRED_AGENT_IMAGE:
        log.warning(
            "team %s: dev node %s uses %r, which no longer exists — the all-in-one "
            "agent image was split into cyberarena/agent-base + one "
            "cyberarena/agent-<provider> per harness. Set the node's image to %r "
            "(built by `bash scripts/build_images.sh`).",
            team_id, dev, image, expected,
        )
        return
    log.warning(
        "team %s: dev node %s uses image %r but its agent provider expects %r; the "
        "harness CLI is baked per image, so unless %r derives from %r install() "
        "will fail with 'not in image'.",
        team_id, dev, image, expected, image, expected,
    )


def _from_dict(raw: dict) -> GameConfig:
    teams_raw = raw.get("teams", [])
    teams = [
        Team(id=t["id"], dev=t["dev"], prod=t["prod"], token=t.get("token", ""))
        for t in teams_raw
    ]
    nodes = [
        Node(
            id=n["id"],
            image=n["image"],
            networks=list(n.get("networks", [])),
            ports=list(n.get("ports", [])),
            install_list=list(n.get("install_list", [])),
            env=dict(n.get("env", {})),
            extra_hosts=dict(n.get("extra_hosts", {})),
        )
        for n in raw.get("nodes", [])
    ]
    networks = [
        Network(name=name, subnet=cfg["subnet"], internal=bool(cfg.get("internal", False)))
        for name, cfg in raw.get("networks", {}).items()
    ]
    topology = TopologyGraph(teams=teams, nodes=nodes, networks=networks)

    challenges = [
        _challenge_from_dict(i, c)
        for i, c in enumerate(raw.get("challenges", []))
    ]

    rc = raw.get("round_config", {})
    mode = str(rc.get("mode", "sync"))
    if mode not in {"sync", "async", "solo"}:
        raise ValueError(
            f"round_config.mode must be 'sync', 'async', or 'solo', got {mode!r}"
        )
    rounds = RoundConfig(
        defense_secs=int(rc.get("defense_secs", 30)),
        attack_secs=int(rc.get("attack_secs", 30)),
        total_rounds=int(rc.get("total_rounds", 3)),
        mode=mode,
        round_secs=int(rc.get("round_secs", 60)),
        solo_role=str(rc.get("solo_role", "")),
    )

    agents: dict[str, AgentConfig] = {}
    for t in teams_raw:
        ag = t.get("agent")
        if ag is None:
            continue
        _validate_agent_block(t["id"], ag)
        cfg = AgentConfig(
            cli=ag["cli"],
            provider=ag.get("provider", ""),
            model=ag.get("model", ""),
            api_key=ag.get("api_key", ""),
            base_url=ag.get("base_url", ""),
            append_system_prompt=ag.get("append_system_prompt", ""),
            security_scanner=bool(ag.get("security_scanner", False)),
        )
        _warn_ignored_cli_flags(t["id"], cfg)
        agents[t["id"]] = cfg

    node_images = {n.id: n.image for n in nodes}
    for t in teams:
        if t.id in agents:
            _warn_dev_image_mismatch(t.id, t.dev, node_images.get(t.dev), agents[t.id])

    fs_raw = raw.get("flag_service", {})
    flag_service = FlagServiceConfig(
        base_url=str(fs_raw.get("base_url", "")),
        container=str(fs_raw.get("container", "")),
    )

    hp_raw = raw.get("health_poller", {})
    recent_default = hp_raw.get("recent_results_default", 10)
    if (
        isinstance(recent_default, bool)
        or not isinstance(recent_default, int)
        or recent_default <= 0
    ):
        raise ValueError(
            "health_poller.recent_results_default must be a positive integer, "
            f"got {recent_default!r}"
        )
    health_poller = HealthPollerConfig(
        base_url=str(hp_raw.get("base_url", "")),
        container=str(hp_raw.get("container", "")),
        recent_results_default=recent_default,
    )

    gw_raw = raw.get("mcp_gateway", {})
    mcp_gateway = MCPGatewayConfig(agent_url=str(gw_raw.get("agent_url", "")))

    ar_raw = raw.get("api_router", {}) or {}
    fw_raw = ar_raw.get("firewall", {}) or {}
    allow_raw = fw_raw.get("allow", {}) or {}
    deny_raw = fw_raw.get("deny", {}) or {}
    upstreams, route_default, route_by_model = _load_router_upstreams(ar_raw)
    api_router = ApiRouterConfig(
        container=str(ar_raw.get("container", "")),
        upstream_url=str(ar_raw.get("upstream_url", "")),
        upstreams=upstreams,
        route_default=route_default,
        route_by_model=route_by_model,
        pricing=_load_router_pricing(ar_raw),
        budgets=_load_router_budgets(ar_raw, {str(t["id"]) for t in teams_raw}),
        firewall=ApiRouterFirewallConfig(
            default=str(fw_raw.get("default", "deny")),
            allow_domains=list(allow_raw.get("domains", [])),
            allow_cidrs=list(allow_raw.get("cidrs", [])),
            deny_domains=list(deny_raw.get("domains", [])),
            deny_cidrs=list(deny_raw.get("cidrs", [])),
        ),
    )

    snapshot_raw = raw.get("snapshot_patched_vulbox", False)
    if not isinstance(snapshot_raw, bool):
        raise ValueError(
            f"snapshot_patched_vulbox must be a boolean, got {snapshot_raw!r}"
        )

    v_raw = raw.get("verifier", {}) or {}
    verifier = VerifierConfig(
        required=bool(v_raw.get("required", True)),
        initial_delay_seconds=int(v_raw.get("initial_delay_seconds", 10)),
        interval_seconds=int(v_raw.get("interval_seconds", 30)),
        timeout_seconds=int(v_raw.get("timeout_seconds", 20)),
        min_blocked_after_restart=int(v_raw.get("min_blocked_after_restart", 2)),
    )

    return GameConfig(
        topology=topology, challenges=challenges, rounds=rounds, agents=agents,
        flag_service=flag_service, health_poller=health_poller,
        mcp_gateway=mcp_gateway, api_router=api_router,
        snapshot_patched_vulbox=snapshot_raw, verifier=verifier,
    )
