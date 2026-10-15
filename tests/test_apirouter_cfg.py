"""End-to-end check on the per-run apirouter config generation chain.

Proves the firewall rules an operator writes in game.yaml actually take effect:

    game.yaml  →  gamemgr.apirouter_cfg.generate()  →  apirouter.config.from_dict
              →  apirouter.firewall.Firewall  →  enforced by the live forward proxy

Unlike tests/test_apirouter_firewall.py (which tests the engine on hand-built
rules) these load the REAL game.yaml, run it through the REAL generator + the
REAL apirouter loader, and drive the REAL forward proxy — so a regression
anywhere in that chain (a renamed field, a dropped section, rules that parse
but don't enforce) fails here. Fully offline: the deny case targets a
non-resolvable .test host; the allow case tunnels to a local echo.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os

import yaml

# game.yaml references these; setdefault so the loader works without a .env.
# UPSTREAM_API_KEY is needed because generate() keeps it as a literal
# ${UPSTREAM_API_KEY} placeholder that apirouter's loader then expands. The two
# team keys get DISTINCT values so the token->principal map doesn't collide
# (apirouter dedupes principals by token).
os.environ.setdefault("ANTHROPIC_API_KEY_TEAM1", "team1-test-key")
os.environ.setdefault("CODEX_API_KEY_TEAM2", "team2-test-key")
for _k in ("CYBERARENA_FACILITY_TOKEN", "API_ROUTER_KEY", "UPSTREAM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import pytest  # noqa: E402

from apirouter.config import from_dict as router_from_dict  # noqa: E402
from apirouter.proxy import start_forward_proxy  # noqa: E402
from gamemgr.apirouter_cfg import (  # noqa: E402
    assign_router_principals,
    generate,
    resolve_upstream_keys,
)
from gamemgr.config import ApiRouterUpstreamConfig  # noqa: E402
from gamemgr.config import _from_dict as game_from_dict  # noqa: E402
from gamemgr.config import load as load_game_config  # noqa: E402

CONFIG = "config/game_ico_v3_isolated_async_mixed.yaml"


def _router_cfg(game_cfg):
    """game.yaml -> generated YAML -> parsed back by apirouter's own loader."""
    return router_from_dict(yaml.safe_load(generate(game_cfg)))


def test_generated_config_is_valid_apirouter_config():
    """The generated YAML loads cleanly in apirouter's own loader, with the
    firewall + upstream + principals it derived from game.yaml."""
    game_cfg = load_game_config(CONFIG)
    raw = yaml.safe_load(generate(game_cfg))
    rc = router_from_dict(raw)

    assert rc.upstream.base_url  # required by apirouter; would raise if missing
    # principals are auto-derived (one per team with an agent key), in order.
    # (asserted on the generated list, not router_from_dict's token->principal
    # map, which dedupes by token — and the placeholder keys other test files
    # set can collide; real per-team keys are distinct.)
    assert [p["id"] for p in raw["principals"]] == ["team1", "team2"]
    # firewall mirrors game.yaml's api_router.firewall
    fw_cfg = game_cfg.api_router.firewall
    assert rc.firewall.default == fw_cfg.default
    assert rc.firewall.allow_domains == fw_cfg.allow_domains


def test_declared_principal_tokens_are_authoritative():
    """A token declared in agent.api_key is used verbatim — it's a plain per-team label,
    not overwritten. Distinct declared tokens flow through to the generated principals."""
    game_cfg = load_game_config(CONFIG)
    for tid, tok in (("team1", "alpha-tok"), ("team2", "beta-tok")):
        game_cfg.agents[tid] = dataclasses.replace(game_cfg.agents[tid], api_key=tok)
    assigned = assign_router_principals(game_cfg)

    assert assigned == {"team1": "alpha-tok", "team2": "beta-tok"}   # kept as declared
    assert {tid: ac.api_key for tid, ac in game_cfg.agents.items()} == assigned
    raw = yaml.safe_load(generate(game_cfg))
    assert {(p["id"], p["token"]) for p in raw["principals"]} == {
        ("team1", "alpha-tok"), ("team2", "beta-tok")}


def test_empty_api_key_gets_a_random_token():
    """A routed team that declares no token gets a RANDOM cad-<hex> token — not the
    guessable cad-<team_id>, so one adversarial agent can't present a rival team's token
    to bill against it. Distinct per team, and different on every run."""
    def run_once():
        game_cfg = load_game_config(CONFIG)
        for tid in game_cfg.agents:
            game_cfg.agents[tid] = dataclasses.replace(game_cfg.agents[tid], api_key="")
        return assign_router_principals(game_cfg)

    a = run_once()
    assert set(a) == {"team1", "team2"}
    assert all(t.startswith("cad-") for t in a.values())    # recognizable as a principal
    assert a["team1"] != "cad-team1" and a["team2"] != "cad-team2"   # NOT guessable
    assert a["team1"] != a["team2"]                          # distinct per team
    assert run_once()["team1"] != a["team1"]                 # random: differs across runs


def test_duplicate_principal_tokens_are_rejected():
    """Two teams on the SAME token collapse into one principal and lose attribution —
    rejected loudly rather than silently mis-billed."""
    game_cfg = load_game_config(CONFIG)
    for tid in game_cfg.agents:
        game_cfg.agents[tid] = dataclasses.replace(game_cfg.agents[tid], api_key="SHARED")
    with pytest.raises(ValueError, match="distinct per team"):
        assign_router_principals(game_cfg)


def test_missing_upstream_key_aborts_the_run(monkeypatch):
    """There is NO keyless/passthrough mode. The router forwards with the upstream's
    key; a team's token is a billing principal, not a provider credential, so an
    upstream without a key has nothing valid to send. Previously this silently
    downgraded to "forward the agent's key" and the run died later as a full match
    of AGENT_FAILED — now it aborts at startup, where the fix is one .env line."""
    game_cfg = load_game_config(CONFIG)
    router = next(n for n in game_cfg.topology.nodes
                  if n.id == game_cfg.api_router.container)
    router.env.pop("UPSTREAM_API_KEY", None)          # node doesn't supply it...
    monkeypatch.delenv("UPSTREAM_API_KEY", raising=False)   # ...and neither does .env

    with pytest.raises(ValueError, match="no API key for upstream"):
        resolve_upstream_keys(game_cfg)


def test_key_resolved_from_dotenv_without_touching_game_yaml(monkeypatch):
    """The intended setup: name the key in .env by convention and declare nothing in
    game.yaml. resolve_upstream_keys injects it into the router node."""
    game_cfg = _multi_upstream_game_cfg(explicit_key_env=False)
    router = next(n for n in game_cfg.topology.nodes
                  if n.id == game_cfg.api_router.container)
    monkeypatch.setenv("API_ROUTER_KEY_ANTHROPIC", "sk-ant-real")
    monkeypatch.setenv("API_ROUTER_KEY_PPAPI", "sk-ppapi-real")

    injected = resolve_upstream_keys(game_cfg)

    assert set(injected) == {"API_ROUTER_KEY_ANTHROPIC", "API_ROUTER_KEY_PPAPI"}
    # the container will now be started with the real keys in its env
    assert router.env["API_ROUTER_KEY_ANTHROPIC"] == "sk-ant-real"
    # ...while the generated config on disk carries only the placeholder
    raw = yaml.safe_load(generate(game_cfg))
    assert raw["upstreams"]["anthropic"]["api_key"] == "${API_ROUTER_KEY_ANTHROPIC}"


def test_node_env_wins_over_dotenv(monkeypatch):
    """A key already declared on the node (the pre-existing style) is left alone —
    resolve only fills gaps, so existing game.yaml files keep working."""
    game_cfg = load_game_config(CONFIG)
    router = next(n for n in game_cfg.topology.nodes
                  if n.id == game_cfg.api_router.container)
    router.env["UPSTREAM_API_KEY"] = "from-node"
    monkeypatch.setenv("UPSTREAM_API_KEY", "from-dotenv")

    assert resolve_upstream_keys(game_cfg) == []       # nothing to inject
    assert router.env["UPSTREAM_API_KEY"] == "from-node"


def test_custom_api_key_env_drives_the_whole_chain(monkeypatch):
    """A custom `api_key_env` (override, not the API_ROUTER_KEY_<ID> convention) must be
    honoured end to end: resolve reads THAT var, the container gets it, and the generated
    config's placeholder names THAT var — not the convention name. (Live-verified against
    ppapi separately; this pins the wiring so CI catches a regression.)"""
    game_cfg = load_game_config(CONFIG)
    router = next(n for n in game_cfg.topology.nodes
                  if n.id == game_cfg.api_router.container)
    router.env.pop("UPSTREAM_API_KEY", None)   # force the custom var to be the only source
    game_cfg = dataclasses.replace(game_cfg, api_router=dataclasses.replace(
        game_cfg.api_router, upstream_url="", route_default="custom", route_by_model=(),
        upstreams={"custom": ApiRouterUpstreamConfig(
            base_url="https://app-us.ppapi.ai", api_key_env="MY_WEIRD_VAR")}))
    router = next(n for n in game_cfg.topology.nodes if n.id == game_cfg.api_router.container)
    monkeypatch.setenv("MY_WEIRD_VAR", "sk-secret-real")
    monkeypatch.setenv("API_ROUTER_KEY_CUSTOM", "sk-convention-should-be-ignored")

    injected = resolve_upstream_keys(game_cfg)

    assert injected == ["MY_WEIRD_VAR"]                        # the custom var, resolved
    assert "API_ROUTER_KEY_CUSTOM" not in router.env            # the convention name is NOT used
    assert router.env["MY_WEIRD_VAR"] == "sk-secret-real"      # real value in the container env
    raw = yaml.safe_load(generate(game_cfg))
    assert raw["upstreams"]["custom"]["api_key"] == "${MY_WEIRD_VAR}"   # placeholder, custom name
    assert "sk-secret-real" not in generate(game_cfg)          # never the value on disk
    # apirouter's own loader expands ${MY_WEIRD_VAR} from its container env
    rc = router_from_dict(raw)
    assert rc.upstream_for("anything")[1].api_key == "sk-secret-real"


def _multi_upstream_game_cfg(*, explicit_key_env=True):
    """game.yaml with two providers on separate keys, routed by model.

    `explicit_key_env=False` leaves api_key_env at the convention the game-config
    loader applies (`API_ROUTER_KEY_<ID>`) — the zero-config .env path.
    """
    def key_env(upstream_id, override):
        return override if explicit_key_env else f"API_ROUTER_KEY_{upstream_id.upper()}"

    game_cfg = load_game_config(CONFIG)
    return dataclasses.replace(
        game_cfg,
        api_router=dataclasses.replace(
            game_cfg.api_router,
            upstream_url="",   # the shorthand and the table are mutually exclusive
            upstreams={
                "anthropic": ApiRouterUpstreamConfig(
                    base_url="https://api.anthropic.com",
                    api_key_env=key_env("anthropic", "ANTHROPIC_API_KEY_TEAM1")),
                "ppapi": ApiRouterUpstreamConfig(
                    base_url="https://app-us.ppapi.ai",
                    api_key_env=key_env("ppapi", "UPSTREAM_API_KEY")),
            },
            route_default="ppapi",
            route_by_model=(("claude-*", "anthropic"),),
        ),
    )


def test_generate_emits_routing_table_apirouter_accepts(monkeypatch):
    """game.yaml's multi-provider block survives the generator AND apirouter's own
    loader — the chain that decides which key pays for a claude-* call."""
    raw = yaml.safe_load(generate(_multi_upstream_game_cfg()))
    assert "upstream" not in raw   # never both spellings; apirouter would reject
    # keys stay as ${VAR} placeholders on disk, expanded from the container env
    assert raw["upstreams"]["anthropic"]["api_key"] == "${ANTHROPIC_API_KEY_TEAM1}"

    # Pin the value here rather than trusting the module-level setdefault: another
    # test module may have won that race, and what we assert is that the placeholder
    # expands from the environment — not what some other file happened to put in it.
    monkeypatch.setenv("ANTHROPIC_API_KEY_TEAM1", "sk-anthropic-real")
    rc = router_from_dict(raw)
    assert rc.upstream_for("claude-opus-4-8")[1].base_url == "https://api.anthropic.com"
    assert rc.upstream_for("claude-opus-4-8")[1].api_key == "sk-anthropic-real"
    assert rc.upstream_for("gpt-4o")[1].base_url == "https://app-us.ppapi.ai"


@pytest.mark.parametrize("upstream_id,expected", [
    ("ppapi", "API_ROUTER_KEY_PPAPI"),
    ("my-vllm", "API_ROUTER_KEY_MY_VLLM"),      # hyphen — illegal in an env var name
    ("openai.eu", "API_ROUTER_KEY_OPENAI_EU"),  # dot — same
])
def test_convention_key_env_is_always_a_legal_env_var_name(upstream_id, expected):
    """Upstream ids are free-form, env var names are not: anything outside [A-Z0-9_]
    cannot be exported from a shell. A naive upper() would derive a name the operator
    is physically unable to set, and the run would abort on a missing key they had no
    way to supply."""
    cfg = game_from_dict({
        "networks": {}, "nodes": [], "teams": [],
        "api_router": {"container": "r", "upstreams": {upstream_id: {"base_url": "https://x"}}},
    })
    name = cfg.api_router.upstreams[upstream_id].api_key_env
    assert name == expected
    assert name.replace("_", "").isalnum() and not name[0].isdigit()


def test_explicit_api_key_env_overrides_the_convention():
    cfg = game_from_dict({
        "networks": {}, "nodes": [], "teams": [],
        "api_router": {"container": "r", "upstreams": {
            "ppapi": {"base_url": "https://x", "api_key_env": "MY_EXISTING_VAR"}}},
    })
    assert cfg.api_router.upstreams["ppapi"].api_key_env == "MY_EXISTING_VAR"


def test_game_yaml_rejects_upstream_url_plus_routes():
    """Same rule at the game.yaml layer as inside apirouter: the shorthand and the
    routing table cannot coexist, so 'which one wins?' never becomes a question."""
    with pytest.raises(ValueError, match="cannot be combined"):
        game_from_dict({
            "networks": {}, "nodes": [], "teams": [],
            "api_router": {
                "container": "api-router",
                "upstream_url": "https://app-us.ppapi.ai",
                "upstreams": {"a": {"base_url": "https://a.example"}},
            },
        })


def test_generated_firewall_decisions_match_game_yaml():
    """The decisions the firewall makes are exactly what game.yaml's allow/deny
    rules imply — the load-bearing assertion that editing game.yaml changes
    what the agent can reach."""
    fw = _router_cfg(load_game_config(CONFIG)).firewall

    # allowed: the two domains game.yaml lists (incl. the *.ppapi.ai glob)
    assert fw.decide("app-us.ppapi.ai").allowed
    assert fw.decide("api.anthropic.com").allowed
    # allowed: docker-internal CIDR (10.0.0.0/8) — decided on resolved ip
    assert fw.decide("some-host", ip="10.1.2.3").allowed
    # denied: anything not listed, under default:deny. registry.npmjs.org is now among
    # them — runtime CLI install is gone, so npm egress was dropped from the allow-list
    # (which also closes the online path for re-fetching a removed scanner).
    assert not fw.decide("registry.npmjs.org").allowed
    assert not fw.decide("evil.com").allowed
    assert not fw.decide("github.com").allowed
    assert not fw.decide("some-host", ip="8.8.8.8").allowed


def _run_proxy_connect(firewall, target: str):
    """Drive the live forward proxy with `firewall`; issue one CONNECT to
    `target`; return (status_line, events)."""
    async def go():
        events: list[dict] = []
        proxy = await start_forward_proxy("127.0.0.1", 0, firewall, on_event=events.append)
        pport = proxy.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", pport)
        writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        await writer.drain()
        status = await reader.readline()
        writer.close()
        proxy.close()
        return status, events

    return asyncio.run(go())


def test_generated_firewall_denies_unlisted_host_via_live_proxy():
    """A host NOT in game.yaml's allow list is rejected with 403 by the real
    forward proxy built from the generated config. `.test` is non-resolvable,
    so this stays offline (ip=None -> default:deny)."""
    fw = _router_cfg(load_game_config(CONFIG)).firewall
    status, events = _run_proxy_connect(fw, "blocked.test:443")
    assert b"403" in status
    assert events and events[0]["allowed"] is False
    assert events[0]["method"] == "CONNECT"


def test_generated_firewall_allows_listed_host_via_live_proxy():
    """A host that game.yaml's allow list covers is let through the real proxy
    (200 tunnel + echo round-trips). We allow 127.0.0.1 via a one-line override
    of the loaded config so the tunnel target is a local echo — proving the
    ALLOW path is enforced end-to-end, fully offline."""
    game_cfg = load_game_config(CONFIG)
    # Override only the firewall's allow_domains so the generated config permits
    # our local echo; everything else (generation path, loader) is unchanged.
    allow_local = dataclasses.replace(
        game_cfg,
        api_router=dataclasses.replace(
            game_cfg.api_router,
            firewall=dataclasses.replace(
                game_cfg.api_router.firewall,
                default="deny",
                allow_domains=["127.0.0.1"],
            ),
        ),
    )
    fw = _router_cfg(allow_local).firewall

    async def go():
        # local echo server as the tunnel's far end
        async def echo(reader, writer):
            data = await reader.read(100)
            writer.write(data)
            await writer.drain()
            writer.close()

        echo_srv = await asyncio.start_server(echo, "127.0.0.1", 0)
        eport = echo_srv.sockets[0].getsockname()[1]
        events: list[dict] = []
        proxy = await start_forward_proxy("127.0.0.1", 0, fw, on_event=events.append)
        pport = proxy.sockets[0].getsockname()[1]

        reader, writer = await asyncio.open_connection("127.0.0.1", pport)
        writer.write(f"CONNECT 127.0.0.1:{eport} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        status = await reader.readline()   # 200 Connection Established
        await reader.readline()            # blank line terminating the head
        writer.write(b"hi-tunnel")         # tunnel is now raw -> hits the echo
        await writer.drain()
        echoed = await reader.read(9)
        writer.close()
        proxy.close()
        echo_srv.close()
        return status, echoed, events

    status, echoed, events = asyncio.run(go())
    assert b"200" in status
    assert echoed == b"hi-tunnel"
    assert events and events[0]["allowed"] is True
