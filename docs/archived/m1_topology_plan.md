# Milestone 1 — Local Topology Runs

**目标**：`Orchestrator.setup()` 拉起所有容器与 docker 网络，按 install_list 装包，`docker ps` 可见全部节点 READY；`teardown()` 完全清理。

## 范围

✅ 实现：

- 构建两个基础镜像：`cyberarena/dev-base`、`cyberarena/prod-base`
- `config/game.yaml` 解析为 `TopologyGraph`
- Docker 网络创建（team1_net / team2_net / game_net）
- 容器创建、attach 网络、启动（**不用 systemd**，PID 1 跑 `sleep infinity`）
- `install_list` 执行：容器 running 后由 orchestrator 通过 `docker exec` 跑 apt/pip install，**在 pre-flight 之前**
- pre-flight：打印两行（design §2 stub）
- 定义 `Service` ABC（接口only，不实现具体 service）
- `setup()` / `teardown()` 完整生命周期
- CLI：`main.py setup` / `main.py teardown`

⬜ 不实现：

- `network.set_cross_team_network()` — 留接口 stub（M3）
- 具体 Service 实现（M2）
- Agent 安装、Round Loop、Flag 提交、Panel（M2–M6）

## 基础镜像

每场比赛都用得到、不变的依赖**烤进镜像**；每队/每场可变的放 `install_list`。

| 镜像 | 内容 |
|------|------|
| `cyberarena/dev-base` | Ubuntu 24.04 + Agent 运行时（python3, pip, websockets, requests）+ 部署工具（rsync, openssh-client, curl）+ 攻击工具（nmap, netcat-openbsd, sqlmap, pwntools, gobuster）+ 调试工具（vim, tmux, tcpdump, strace） |
| `cyberarena/prod-base` | Ubuntu 24.04 + python3 + openssh-server |

## Service ABC（M1 只定义）

```python
class Service(ABC):
    name: str
    port: int | None
    protocol: str | None
    install(container) -> None
    start(container) -> None
    stop(container) -> None
    is_alive(container) -> bool
```

prod 容器跑的服务通过实现该 ABC 接入；M2 提供首个实现（端口旧 repo 的 `challenge_simple.NotesService`）。

## setup() 执行顺序

```
1. 创建 docker networks（team1_net / team2_net / game_net）
2. 按序创建 + 启动容器，attach 网络
3. 对每个节点 docker exec 跑 install_list（apt / pip）
4. pre-flight 打印两行
```

`teardown()` 反向：stop containers → remove containers → remove networks。

## 任务拆分

| # | 任务 | 输出 |
|---|------|------|
| 1 | `config/docker/Dockerfile.dev` + `Dockerfile.prod` | 两份 Dockerfile |
| 2 | `scripts/build_images.sh` | 一键构建两个镜像 |
| 3 | `config/game.yaml`（teams + nodes + networks，含 install_list 字段） | YAML |
| 4 | `models/topology.py`（Node / Edge / TopologyGraph dataclass） | 模块 |
| 5 | `orchestrator/topology.py`（YAML → TopologyGraph） | 模块 + 单测 |
| 6 | `orchestrator/infra/network.py`（create / remove，`set_cross_team_network` stub） | 模块 |
| 7 | `orchestrator/infra/docker.py`（container create / start / stop / remove / exec） | 模块 |
| 8 | `orchestrator/service.py`（Service ABC） | 模块 |
| 9 | `orchestrator/orchestrator.py`（编排 setup / teardown） | 模块 |
| 10 | `main.py`（argparse `setup` / `teardown`） | CLI |
| 11 | `tests/test_m1_topology.py` 集成测试 | pytest |
| 12 | `requirements.txt` | 依赖清单 |

## 验证

```bash
./scripts/build_images.sh
docker images | grep cyberarena              # 期望：dev-base + prod-base

python main.py setup
docker ps                                    # 期望：4 个容器全部 Up
docker network ls | grep -E 'team[12]_net|game_net'
docker inspect team1_prod                    # 期望：连接 team1_net + game_net
docker exec team1_dev which nmap             # 期望：/usr/bin/nmap

python main.py teardown
docker ps -a | grep team                     # 期望：无输出
docker network ls | grep -E 'team[12]_net'   # 期望：无输出

pytest tests/test_m1_topology.py                      # 期望：全绿
```

## 目录结构（M1 完成后）

```
CyberArenaOrchestration/
├── config/
│   ├── game.yaml
│   └── docker/
│       ├── Dockerfile.dev
│       └── Dockerfile.prod
├── models/
│   └── topology.py
├── orchestrator/
│   ├── __init__.py
│   ├── orchestrator.py
│   ├── topology.py
│   ├── service.py
│   └── infra/
│       ├── __init__.py
│       ├── docker.py
│       └── network.py
├── scripts/
│   └── build_images.sh
├── tests/
│   └── test_m1_topology.py
├── main.py
├── requirements.txt
└── docs/
    ├── design_v1.md
    ├── m1_topology_plan.md
    └── m1_topology_report.md
```

## 依赖

- Python 3.11+
- Docker Engine / Docker Desktop（Ubuntu 20.10+ / macOS）
- Python 包：`docker`、`pyyaml`、`pytest`
