# Milestone 1 — Local Topology Runs — Execution Report

**Goal:** `Orchestrator.setup()` brings up all containers and docker networks; `docker ps` shows all nodes READY; `teardown()` cleans up fully.

**Status:** Complete. All verification steps in [m1_topology_plan.md](m1_topology_plan.md) pass.

## What was built

| Path | Purpose |
|------|---------|
| `config/docker/Dockerfile.dev` | `cyberarena/dev-base` image — Ubuntu 24.04 + agent runtime (python3, pip, websockets, requests) + deploy tools (rsync, openssh-client, curl) + attack tools (nmap, netcat-openbsd, sqlmap, gobuster, pwntools) + debug tools (vim, tmux, tcpdump, strace) |
| `config/docker/Dockerfile.prod` | `cyberarena/prod-base` image — Ubuntu 24.04 + python3 + openssh-server |
| `scripts/build_images.sh` | One-shot builder for both images |
| `config/game.yaml` | Single match config — teams + nodes + networks + install_list |
| `models/topology.py` | `Node` / `Network` / `Team` / `TopologyGraph` dataclasses |
| `orchestrator/topology.py` | YAML -> `TopologyGraph` loader |
| `orchestrator/service.py` | `Service` ABC + `ContainerHandle` (interface only; concrete services land in M2) |
| `orchestrator/infra/network.py` | Docker network create / remove; `set_cross_team_network` stub (M3) |
| `orchestrator/infra/docker.py` | Container create / start / stop / remove / exec; multi-network attach |
| `orchestrator/orchestrator.py` | `setup()` (networks -> containers -> install_list -> pre-flight) and `teardown()` |
| `main.py` | CLI: `setup` / `teardown` |
| `tests/test_m1_topology.py` | Integration test: parses topology, runs setup, asserts container + network state, runs teardown, asserts cleanup |
| `requirements.txt` | `docker`, `pyyaml`, `pytest` |

## Architectural decisions (locked in M1)

- **No systemd inside containers.** PID 1 runs `sleep infinity`; lifecycle is driven from the host via `docker exec`.
- **Two-tier image strategy.** Match-invariant tools baked into base images; per-match / per-team tools come from `install_list` (executed by orchestrator after containers start, before pre-flight).
- **Pluggable Service interface.** `prod` containers will host services that implement the `Service` ABC. M2 will port the reference repo's `notes` service as the first concrete implementation; swapping in DVWA or other web services requires only a new `Service` subclass.
- **Network backend abstraction.** `set_cross_team_network(allow: bool)` is the only entry point GameMgr will use; the docker-network backend lands in M3.

## Verification

All commands run from repo root inside the project venv.

### Image build

```
./scripts/build_images.sh
docker images | grep cyberarena
```

Result:
```
cyberarena/dev-base    latest   9ade5f2e84b2   672MB
cyberarena/prod-base   latest   bf16e2fa28de   213MB
```

### setup

```
python3 main.py setup
```

Log excerpt:
```
orchestrator setup: 3 networks, 4 nodes
created network team1_net (10.1.0.0/24)
created network team2_net (10.2.0.0/24)
created network game_net (10.0.0.0/24)
created container team1_dev (image=cyberarena/dev-base)
created container team1_prod (image=cyberarena/prod-base)
connected team1_prod to game_net
created container team2_dev (image=cyberarena/dev-base)
created container team2_prod (image=cyberarena/prod-base)
connected team2_prod to game_net
pre-flight: starting checks
pre-flight: all checks passed
orchestrator setup complete
```

Container state:
```
$ docker ps --filter "name=team" --format "{{.Names}} {{.Status}} {{.Image}}"
team1_dev    Up   cyberarena/dev-base
team1_prod   Up   cyberarena/prod-base
team2_dev    Up   cyberarena/dev-base
team2_prod   Up   cyberarena/prod-base
```

Network attachment (prod containers correctly on both `teamN_net` and `game_net`):
```
$ docker inspect team1_prod --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}({{$v.IPAddress}}) {{end}}'
game_net(10.0.0.2) team1_net(10.1.0.3)
```

Dev-base tooling baked in:
```
$ docker exec team1_dev which nmap
/usr/bin/nmap
```

### teardown

```
python3 main.py teardown
```

Post-teardown checks: no `team*` containers remain; no `team{1,2}_net` or `game_net` networks remain.

### pytest

```
$ pytest tests/test_m1_topology.py -v
tests/test_m1_topology.py::test_topology_parse        PASSED
tests/test_m1_topology.py::test_setup_then_teardown   PASSED
2 passed in 13.68s
```

## Deferred to later milestones

| Item | Milestone |
|------|-----------|
| `NetworkManager.set_cross_team_network()` real implementation | M3 |
| Concrete `Service` implementation (port `notes` from reference repo) | M2 |
| `install_list` populated with real packages (currently empty in `game.yaml`) | M2+, as needed |
| Pre-flight: actual checks (topology DAG, image pullable, port collisions, agent reachability) | M2+ |
| Agent install, Round Loop, Flag submit, Panel | M2–M6 |