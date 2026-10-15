# Milestone 3 — Network Switching Works

**目标**：`orchestrator.network.set_cross_team_network(allow)` 真实切换 `game_net` 跨队连通性。DEFENSE 阶段两队 prod 互相不可达；ATTACK 阶段恢复双向可达。RoundLoop 不再吞 `NotImplementedError`，相位切换直接走真实实现。

## 范围

✅ 实现：

- `NetworkManager.set_cross_team_network(allow)` 真实切换 —— 实现策略：`docker network disconnect game_net <prod_container>` / `connect`
- NetworkManager 在 Orchestrator 构造时拿到 topology，自动推导出"挂在 `game_net` 上的容器列表"（即两队 prod），不再硬编码 / 不依赖 caller 传参
- 幂等：连续两次 `set_cross_team_network(False)` 或 `(True)` 都安全；用 `container.reload()` + 网络挂载状态判断
- `RoundLoop` 去掉 `try/except NotImplementedError` 的 stub 处理，让真实错误传播
- 集成测试：直接调 `set_cross_team_network` 验证连通性；并跑一次 round loop 验证相位切换确实改变可达性
- README / m2 报告里"M3 deferred"那行更新成 done

⬜ 不实现：

- `iptables` backend（未来 Linux 优化，避开 reconnect 短暂中断；接口已经预留）
- 静态 IP 分配（reconnect 后 IP 可能漂移）—— MVP 接受
- Reconnect 中断的零丢包处理

## 连通性模型

**M3 只操作 `game_net` 一张网，仅隔离跨队 prod↔prod 流量。其它链路在任何相位都不动。**

| 链路 | DEFENSE | ATTACK | 走的网络 | M3 是否插拔 |
|------|---------|--------|----------|-------------|
| 跨队 prod ↔ prod | ❌ 断开 | ✅ 双向 | `game_net` | ✅ 唯一会动的链路 |
| **跨队 dev ↔ 任何（dev/prod）** | ❌ **始终不可达** | ❌ **始终不可达** | 无共享 docker 网络 | ❌ 结构性隔离 |
| 队内 dev ↔ prod | ✅ | ✅ | `team{N}_net` | ❌ |
| 队内 prod ↔ prod（自己 dev 跟自己 prod） | ✅ | ✅ | `team{N}_net` | ❌ |
| Orchestrator host ↔ 任何容器 | ✅ | ✅ | docker SDK / port mapping | ❌ |

`set_cross_team_network` 只对 prod 容器在 `game_net` 上的 endpoint 做 connect/disconnect，**不要碰** `team{N}_net`。NetworkManager 推导 service container 列表时显式过滤"包含 `game_net`"，不要顺手把 dev 容器一起处理。

### 跨队 dev 隔离：禁止 agent ↔ agent 直接攻击

两个 agent（分别跑在 team1_dev / team2_dev）必须**任何相位都不能直接互相访问对方容器**——攻击只能通过 prod↔prod via `game_net` 这条受控通道。这是为了让游戏机制不被旁路：

1. **Docker 网络层结构性隔离**：dev 容器只挂自己的 `team_net`，永远不在 `game_net` 上，所以跨队 dev↔anything 没有共享网络，docker DNS / IP 都无解。
2. **Host port mapping 旁路已封堵**：`game.yaml` 里 prod 的端口绑定用 `127.0.0.1:8001:5000`（仅 host loopback）。否则 dev 容器经默认网关到 host 后能直达 `host:8002` → team2_prod，绕过 team_net 隔离。
3. **集成测试断言**：M3 测试里 assert `team1_dev → team2_prod`、`team1_dev → team2_dev` 在任何相位都失败（不仅是 DEFENSE）。

### TCP reset 行为

Docker 的 `network disconnect` 会 detach endpoint、更新 container 的 network namespace；**任何已建立的、走该 endpoint 的 TCP 连接都可能被 reset**。M3 显式接受这点：

- 只保证 **新连接的 reachability policy**（DEFENSE 拒、ATTACK 通）
- 不保证已有连接存活（agent 实现要把 reset / EPIPE 当 expected）
- M3 不引入零中断方案；将来若要无缝切换，走 iptables backend（接口已预留）

## 实现要点

- `NetworkManager.__init__(client, graph)` 新增 `graph` 参数；从中推导 `self._service_containers = [n.id for n in graph.nodes if "game_net" in n.networks]`
- `set_cross_team_network(allow)`：
  ```python
  net = self._client.networks.get("game_net")
  for name in self._service_containers:
      container = self._client.containers.get(name)
      container.reload()
      attached = "game_net" in container.attrs["NetworkSettings"]["Networks"]
      if allow and not attached:
          net.connect(container)
      elif not allow and attached:
          net.disconnect(container)
  ```
- 错误处理：单个容器不存在视为已清理（log warn，继续）；其它 docker API 错误抛出
- 接口签名不变：`set_cross_team_network(allow: bool) -> None` —— GameMgr / RoundLoop 调用点零改动（除了去掉 try/except）

## 任务拆分

| # | 任务 | 输出 |
|---|------|------|
| 1 | `orchestrator/infra/network.py`：实现 `set_cross_team_network`，构造接收 graph | 模块 |
| 2 | `orchestrator/orchestrator.py`：把 graph 传给 NetworkManager | 模块 |
| 3 | `gamemgr/round.py`：移除 `try/except NotImplementedError` | 模块 |
| 4 | `tests/test_m3_network.py`：集成测试 —— 直接切换 + 验证跨队 prod↔prod 可达性 | pytest |
| 5 | `docs/m2_gameloop_report.md`：把"M3 deferred"行更新成已完成 | docs |

## 验证

```bash
# 起 setup（不进游戏循环）
python3 main.py setup

# 默认状态：两队 prod 都接在 game_net 上，应当可达
docker exec team1_prod python3 -c \
  "import socket; s=socket.socket(); s.settimeout(2); s.connect(('team2_prod', 5000)); print('OK')"
# 期望：OK

# 用 python 驱动 set_cross_team_network(False)
python3 -c "
from orchestrator import topology
from orchestrator.infra.network import NetworkManager
g = topology.load('config/game.yaml')
NetworkManager(graph=g).set_cross_team_network(False)
"

# 再试跨队连通：应失败
docker exec team1_prod python3 -c \
  "import socket; s=socket.socket(); s.settimeout(2);
   try: s.connect(('team2_prod', 5000)); print('UNEXPECTED OK')
   except Exception as e: print('blocked:', type(e).__name__)"
# 期望：blocked: gaierror（DNS 失败，因为共享网络没了）或 timeout

# 切回 ATTACK
python3 -c "
from orchestrator import topology
from orchestrator.infra.network import NetworkManager
g = topology.load('config/game.yaml')
NetworkManager(graph=g).set_cross_team_network(True)
"

# 再试：应通
docker exec team1_prod python3 -c \
  "import socket; s=socket.socket(); s.settimeout(2); s.connect(('team2_prod', 5000)); print('OK')"
# 期望：OK

python3 main.py teardown

pytest tests/test_m3_network.py            # 全绿
pytest tests/test_m1_topology.py tests/test_m2_gameloop.py   # M1/M2 不回归
```

集成测试 `tests/test_m3_network.py` 覆盖：

1. `setup → set(False) → 跨队不可达 → set(True) → 跨队可达 → teardown`（直接测网络层）
2. 连续两次 `set(False)` 不抛错（幂等）
3. 跑短 round loop（defense=1s / attack=1s / total=2），在 DEFENSE 期间 sample 一次连通性应失败，在 ATTACK 期间 sample 应成功

## 目录结构（M3 完成后）

```
CyberArenaOrchestration/
├── orchestrator/
│   └── infra/
│       └── network.py            # set_cross_team_network 真实实现
├── tests/
│   ├── test_m1_topology.py
│   ├── test_m2_gameloop.py
│   └── test_m3_network.py        # 新增
└── docs/
    ├── design_v1.md
    ├── m1_topology_plan.md
    ├── m1_topology_report.md
    ├── m2_gameloop_plan.md
    ├── m2_gameloop_report.md
    └── m3_network_plan.md        # 本文档
```

## 依赖

无新增。`docker` SDK 已在 M1 引入。