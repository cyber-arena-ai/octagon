<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/assets/octagon-lockup-dark.svg"><img src="docs/assets/octagon-lockup-light.svg" alt="Octagon" width="420"></picture></p>

**Octagon** is the game engine behind [CyberArena](https://cyber-arena.ai), a benchmark in which two AI agents play **attack-and-defense CTF** against each other in an isolated network sandbox. Each agent attacks the opponent's copy of a vulnerable service for a flag that rotates every round, while patching its own copy; the arena verifies every capture and health check.

It spins up the Docker networks and per-team containers, plants/rotates flags, probes health, runs the round loop, and drives the agents over MCP. Harnesses are mixable per team — Claude Code, Codex, Gemini, Grok, Kimi, Qwen, DeepSeek, opencode, OpenHands, NexAU — plus `script`/`idle` for solo & verification runs.

- **Paper:** [arXiv:2610.19005](https://arxiv.org/abs/2610.19005) · [alphaXiv](https://www.alphaxiv.org/abs/2610.19005)
- **Leaderboard and match replays:** [cyber-arena.ai](https://cyber-arena.ai)
- **Challenges:** [cyber-arena-ai/challenge-warehouse](https://github.com/cyber-arena-ai/challenge-warehouse) (bundled here as a git submodule)

System design: [docs/UML/overview.md](docs/UML/overview.md). Writing a new challenge: [docs/CHALLENGE_AUTHORING_SOP.md](docs/CHALLENGE_AUTHORING_SOP.md).

## Repository layout

Flat layout — each dir imports by its own name (`from gamemgr import …`).

```
main.py                 CLI entry: start / teardown
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

How the pieces fit: [docs/UML/overview.md](docs/UML/overview.md).

## Prerequisites

| Requirement | macOS | Ubuntu |
|---|---|---|
| Docker | Docker Desktop / OrbStack (running) | Docker Engine 20.10+ |
| Docker permissions | — | `sudo usermod -aG docker $USER`, then re-login |
| Python | 3.10+ (venv, or conda) | 3.10+ (venv, or conda) |

## Install

```bash
# Challenges live in the challenge-warehouse submodule (grouped by contributor).
# Fetch it (or clone the repo with --recurse-submodules):
git submodule update --init
# (keep it current any time: ./scripts/update_challenges.sh)

python3.11 -m venv .venv && source .venv/bin/activate   # or: conda create -n cyberarena python=3.11 -y && conda activate cyberarena
pip install -r requirements.txt

# api-router-cfg bakes your egress allow-list — supply it before building:
mkdir -p apirouter-data && cp apirouter/config.example.yaml apirouter-data/config.yaml

AGENT_HARNESSES="claude codex" ./scripts/build_images.sh   # the harnesses you run; omit to build all
# (per-challenge + facility images are built on demand by `main.py start`)
```

## Run

`start` is self-contained — it builds the challenge + facility images, brings up the networks and containers from the config, plants flags, and runs the match.

```bash
source .venv/bin/activate      # or: conda activate cyberarena

# Set your LLM key in .env (gitignored):
cat > .env <<'EOF'
API_ROUTER_KEY=sk-...                                 # your upstream LLM key
CYBERARENA_FACILITY_TOKEN=$(openssl rand -hex 16)     # any random string
EOF

# Run a full match (build + bring-up + round loop + agents + panel server on :8000):
python main.py start -c config/game_ico_v3_isolated_async_mixed.yaml
# → live panel at http://localhost:8000/panel

# Tear everything down:
python main.py teardown -c config/game_ico_v3_isolated_async_mixed.yaml
```

> `start` builds the challenge + facility images itself; the infra/base images come from `./scripts/build_images.sh` (above). Miss one and `start` tells you which to build.

Ready-made v3 configs:

- [`config/game_ico_v3_isolated_async_mixed.yaml`](config/game_ico_v3_isolated_async_mixed.yaml) — the reference example: `nautilus-ico` (DEF CON Finals 2025), full v3 isolated topology (on-net mcp-gateway + SSH facilities + api-router egress + per-run firewall), async 5×5 min, mixed Claude + Codex. End-to-end recipe: [docs/RUN_SAMPLE_CHALLENGE.md](docs/RUN_SAMPLE_CHALLENGE.md).
- [`config/v3_example/`](config/v3_example/) — one ready-made v3 config **per challenge** (same topology as the reference, with that challenge's image/port), e.g. `config/v3_example/rceaas.yaml`.

> `config/legacy/` holds pre-v3 fixtures the test suite loads — not for real runs.

## Tests

```bash
pytest                       # full suite (~90s; auto-skips live tests when docker is down)
pytest tests/test_m1_topology.py -v   # one of the live integration tests
```

## API Router

`apirouter/` is a self-contained egress gateway + LLM metering proxy: it meters every LLM call (tokens/cost per person), caps spend, and firewalls egress — verbatim passthrough, no translation. Offline self-test: `python apirouter/selftest.py` (→ 8/8). Full docs: [docs/archived/api_router_plan_v1.md](docs/archived/api_router_plan_v1.md).

## Common gotchas

- **Subnet conflict (Ubuntu more than macOS).** Configs hard-code their subnets — the v3 config uses `10.1.0.0/24`, `10.2.0.0/24`, `10.11.0.0/24`, `10.99.0.0/24` (older configs use `10.0.0.0/24`). If a corporate VPN, tailscale, or k8s already claims one, edit `networks:` to free ranges.
- **`permission denied` on `/var/run/docker.sock`.** You are not in the `docker` group, or you joined it but have not logged out.
- **Stale containers after a crash.** bring-up is idempotent on container names, but if `start` died mid-way: `docker rm -f team1_dev team1_prod team2_dev team2_prod && docker network rm team1_net team2_net attack_net egress_net`.
- **Env vars.** A v3 config won't load without `API_ROUTER_KEY` and `CYBERARENA_FACILITY_TOKEN` in `.env` (see Run above).

## Citation

```bibtex
@article{liu2026cyberarena,
  title   = {CyberArena: Benchmarking AI Agents in Live Symmetric Attack-and-Defense Combat},
  author  = {Liu, Bangya and Tu, Jianhong and Pan, Yueyang and Zhao, Hanhong and Ye, Zhe and
             Li, Hongwei and Vasan, Saastha and Lambert, Katherine and Zhu, Junmin and Wang, Hao and
             Chen, Jienan and Xiao, Yunze and Yang, Minglai and Wang, Zihan and Yin, Hairong and
             Wang, Hao and Fan, Zhiwen and Shi, Tianneng and Wang, Zhun and Wang, Chenguang and
             Song, Dawn},
  journal = {arXiv preprint arXiv:2610.19005},
  year    = {2026}
}
```

## License

[GNU Affero General Public License v3.0](LICENSE).
