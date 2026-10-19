<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/assets/octagon-lockup-dark.svg"><img src="docs/assets/octagon-lockup-light.svg" alt="Octagon" width="420"></picture></p>

**Octagon** is the game engine behind [CyberArena](https://cyber-arena.ai), a benchmark in which two AI agents play **attack-and-defense CTF** against each other in an isolated network sandbox. Each agent attacks the opponent's copy of a vulnerable service for a flag that rotates every round, while patching its own copy; the arena verifies every capture and health check.

This repository spins up the Docker networks and per-team containers, plants and rotates flags, probes service health, runs the round loop, and drives LLM coding agents for both sides through an MCP channel. Harnesses are mixable per team: Claude Code, Codex, opencode, Gemini CLI, Grok CLI, Kimi, Qwen Code, the DeepSeek harness, OpenHands and NexAU, plus `script` and `idle` placeholders for solo and verification runs.

- **Paper:** [arXiv:2610.19005](https://arxiv.org/abs/2610.19005) · [alphaXiv](https://www.alphaxiv.org/abs/2610.19005)
- **Leaderboard and match replays:** [cyber-arena.ai](https://cyber-arena.ai)
- **Challenges:** [cyber-arena-ai/challenge-warehouse](https://github.com/cyber-arena-ai/challenge-warehouse) (bundled here as a git submodule)

System design: [docs/UML/overview.md](docs/UML/overview.md). Writing a new challenge: [docs/CHALLENGE_AUTHORING_SOP.md](docs/CHALLENGE_AUTHORING_SOP.md).

## Repository layout

Flat package layout — every directory below is importable by its own name (`from gamemgr import …`). The top level splits into core code, support assets, and generated dirs (`runs/`, git-ignored).

```
main.py                 CLI entry: setup / start / teardown
│
├─ gamemgr/             GameMgr, round loop, Timeline, events, FastAPI + MCP surface, config loader
├─ orchestrator/        Docker container + network lifecycle (infra/ + per-team compose)
├─ flag/                FlagServer + FlagStore — plant / rotate / submit
├─ health/              HealthChecker + HealthPoller — functionality probes
├─ facility/            Facility boundary: status/events/exec ports + SSH/in-process adapters
├─ mcpgateway/          On-net MCP gateway container — agents' tool surface, forwards to GameMgr REST
├─ agents/              Agent managers + transports, one adapter per harness (claude · codex · opencode · …)
├─ challenges/          Challenge contract (interface ABC) + auto-discovery registry
├─ challenge-warehouse/ Challenge implementations — git submodule, grouped by contributor
├─ apirouter/           Egress gateway + LLM metering proxy (self-contained service)
├─ models/              Shared data models (topology, agent config)
│
├─ config/              Game YAML configs + team-image Dockerfiles (docker/)
├─ dockerfiles/         v3 infra/base image build contexts (agent/base + agent/<harness> · mcp-gateway · flag-service · health-poller · api-router-cfg)
├─ panel/               panel.html live dashboard (served at /panel)
├─ vis/                 Realtime topology visualizer — standalone read-only HTML graph
├─ scripts/             Helper scripts (build_images.sh, update_challenges.sh, …)
├─ tests/               Test suite (~1,200 tests; run scoped to tests/ — see below)
├─ docs/                Design docs + UML (docs/UML/)
│
└─ runs/                Generated run artifacts: events.jsonl, panel snapshots          ·  git-ignored
```

Challenge packages live in the `challenge-warehouse` submodule but are stitched into the `challenges` namespace via `__path__`, so each still imports as `challenges.<name>`. How the pieces fit: see [docs/UML/overview.md](docs/UML/overview.md).

## Prerequisites

| Requirement | macOS | Ubuntu |
|---|---|---|
| Docker | Docker Desktop / OrbStack (running) | Docker Engine 20.10+ |
| Docker permissions | — | `sudo usermod -aG docker $USER`, then re-login |
| Python | conda / miniconda (any recent) | conda / miniconda (any recent) |

```bash
docker version --format '{{.Server.Version}}'
conda --version
```

## Setup

```bash
# Challenges live in the challenge-warehouse submodule (grouped by contributor).
# Fetch it (or clone the repo with --recurse-submodules):
git submodule update --init
# Keep it on the latest warehouse main any time: ./scripts/update_challenges.sh

# The bundled submodule is the default. To use a separate checkout for review,
# select it before Python starts:
export CYBERARENA_CHALLENGE_WAREHOUSE=/absolute/path/to/challenge-warehouse

conda create -n cyberarena python=3.11 -y
conda activate cyberarena
pip install -r requirements.txt

./scripts/build_images.sh    # all v3 infra/base images: agent-base + one agent-<harness>
                             # per provider (claude, codex, …) · mcp-gateway ·
                             # flag-service-base · health-poller-base · api-router(+-cfg)
# AGENT_HARNESSES="claude codex" ./scripts/build_images.sh   # only the harnesses you run
# Per-challenge images (cyberarena/chal-notes, cyberarena/chal-nautilus-ico, …) and
# the per-game flag-service/health-poller are built on demand by `main.py start`.
# (api-router-cfg needs apirouter-data/config.yaml — see RUN_SAMPLE_CHALLENGE.md §5.)
```

## Run

`start` is self-contained — it builds the challenge + facility images, brings up the networks and containers from the config, plants flags, and runs the match. **You do not need to run `setup` first.**

```bash
conda activate cyberarena

# Run a full match (build + bring-up + round loop + agents + panel server on :8000):
python main.py start -c config/game_ico_v3_isolated_async_mixed.yaml
# → live panel at http://localhost:8000/panel

# Tear everything down:
python main.py teardown -c config/game_ico_v3_isolated_async_mixed.yaml
```

> `main.py setup` (bring-up only, no match) is **optional and not currently maintained** — its build path doesn't wire the v3 api-router / mcp-gateway / facility containers, so prefer `start`. The infra/base images those configs reference are built once by `./scripts/build_images.sh` (above); `start` itself builds only the per-challenge + per-game facility images. If a base image is missing, `start` aborts immediately with `Required base images not found … run bash scripts/build_images.sh first` rather than failing cryptically mid-bring-up.

Ready-made v3 configs:

- [`config/game_ico_v3_isolated_async_mixed.yaml`](config/game_ico_v3_isolated_async_mixed.yaml) — the reference example: `nautilus-ico` (DEF CON Finals 2025), full v3 isolated topology (on-net mcp-gateway + SSH facilities + api-router egress + per-run firewall), async 5×5 min, mixed Claude + Codex. End-to-end recipe: [docs/RUN_SAMPLE_CHALLENGE.md](docs/RUN_SAMPLE_CHALLENGE.md).
- [`config/v3_example/`](config/v3_example/) — one ready-made v3 config **per challenge** (same topology as the reference, with that challenge's image/port), e.g. `config/v3_example/rceaas.yaml`.

> [`config/legacy/`](config/legacy/) holds `game.yaml` (the minimal pre-v3 topology fixture the test suite loads — not for real runs) and the single-provider nautilus variants `game_ico_v3_{opencode,codex}.yaml`. Other pre-v3 example configs were removed; for real runs use the v3 configs above.

## Tests

```bash
pytest                       # full suite (~90s; auto-skips live tests when docker is down)
pytest tests/test_m1_topology.py -v   # one of the live integration tests
```

## API Router (egress gateway + LLM metering proxy)

`apirouter/` is a self-contained service that sits between containers and the outside world: it meters every LLM call (tokens / cost, per person), caps spend, and firewalls egress by domain/IP. It's verbatim passthrough — no format translation. Full docs: [docs/archived/api_router_plan_v1.md](docs/archived/api_router_plan_v1.md).

```bash
# offline self-test (built-in mock upstream — no key, no network)
pip install -r apirouter/requirements.txt
python apirouter/selftest.py            # → 8/8 checks passed

# run it (config + logs live in one dir; only the real upstream key via -e)
docker build -t cyberarena/api-router apirouter/
mkdir -p apirouter-data && cp apirouter/config.example.yaml apirouter-data/config.yaml
docker run -d --name api-router -p 8080:8080 -p 3128:3128 \
  -e UPSTREAM_API_KEY=sk-real -v "$PWD/apirouter-data:/data" cyberarena/api-router

curl -s localhost:8080/usage | jq      # per-person spend, live
```

## Common gotchas

- **Subnet conflict (Ubuntu more than macOS).** Configs hard-code their subnets — the v3 config uses `10.1.0.0/24`, `10.2.0.0/24`, `10.11.0.0/24`, `10.99.0.0/24` (older configs use `10.0.0.0/24`). If a corporate VPN, tailscale, or k8s already claims one, edit `networks:` to free ranges.
- **`permission denied` on `/var/run/docker.sock`.** You are not in the `docker` group, or you joined it but have not logged out.
- **Stale containers after a crash.** bring-up is idempotent on container names, but if `start` died mid-way: `docker rm -f team1_dev team1_prod team2_dev team2_prod && docker network rm team1_net team2_net attack_net egress_net`.
- **Env vars.** `start` needs the agent keys (`ANTHROPIC_API_KEY_TEAM1`, plus `CODEX_API_KEY_TEAM2` for the mixed config) **and** `CYBERARENA_FACILITY_TOKEN` (any random string — the facility↔GameMgr auth) in `.env`. The token isn't optional: a v3 config references `${CYBERARENA_FACILITY_TOKEN}` and **won't load** if it's unset.

## Citation

```bibtex
@article{liu2026cyberarena,
  title   = {CyberArena: Benchmarking AI Agents in Live Symmetric Attack-and-Defense Combat},
  author  = {Liu, Bangya and Tu, Jianhong and Pan, Yueyang and Zhao, Hanhong and Ye, Zhe and
             Li, Hongwei and Vasan, Saastha and Lambert, Katherine and Zhu, Junmin and Wang, Hao and
             Chen, Jienan and Xiao, Yunze and Yang, Minglai and Wang, Zihan and Yin, Hairong and
             Wang, Hao and Fan, Zhiwen and Shi, Tianneng and Wang, Zhun and Guo, Wenbo and
             Wang, Chenguang and Song, Dawn},
  journal = {arXiv preprint arXiv:2610.19005},
  year    = {2026}
}
```

## License

[GNU Affero General Public License v3.0](LICENSE).
