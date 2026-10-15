# Milestone 2 — Game Loop Runs

**目标**：`GameMgr.start()` 跑通 `DEFENSE ⇄ ATTACK` 状态机；首个具体 `Service`（notes）部署到所有 prod 容器并运行；事件经 Event Bus 推送（WebSocket 客户端可订阅）。网络开关在 M2 走 no-op（M3 接真实切换）。

## 范围

✅ 实现：

- 端口 reference repo 的 `challenge_simple.NotesService` 到我们的 `Service` ABC（M2 首个具体 adapter；后续换其它 web service 只新增 adapter）
- Service registry：从 `game.yaml` 读取 services 配置，按 type 动态实例化（保证接口通用、可替换）
- `gamemgr/events.py`：`Event` / `EventType` / `EventBus`（async pub-sub，内存）
- `gamemgr/round.py`：Round 状态机 `INIT → PREFLIGHT → SETUP → DEFENSE ⇄ ATTACK → STOPPED`
- `gamemgr/game_mgr.py`：`setup()` / `start()` / `stop()`，调用 `orchestrator.network.set_cross_team_network` 切相位（M2 内部仍走 no-op stub）
- `gamemgr/api.py`：FastAPI app — `POST /game/start`、`POST /game/stop`、`GET /game/status`、`GET /game/rounds`、`GET /game/events`、`WS /events/stream`、`POST /game/flag/submit`（M2 仅做骨架，返回 false）
- `main.py` 新增 `start` / `stop`（保留 M1 的 `setup` / `teardown`）

⬜ 不实现：

- `network.set_cross_team_network()` 真正切换 — 仍是 stub（M3）
- 真正的 Flag 植入/验证 — `POST /game/flag/submit` 骨架返回 false（M5）
- Agent 安装、`deploy_complete` 上报 — `last_deploy_time` 取 config 默认值（M4）
- Poller / Healthcheck / Scoreboard — 仅接口
- Panel — M6

## Service registry（接口通用性）

每个 vulnerable service 在 `game.yaml` 里自己声明 `name` / `type` / `port` / `deploy_on`；`type` 是 registry key，框架按 type 找对应 adapter。Notes 只是 M2 的首个示例，换 blog / DVWA / 任意 HTTP service 只需新增 adapter + registry 一行。

```yaml
services:
  - name: notes
    type: notes
    port: 5000
    deploy_on: [team1, team2]   # team id；GameMgr 经 teams 段映射到 prod 容器

  # 以后切换其它 service 只改这里：
  # - name: blog
  #   type: blog
  #   port: 8080
  #   deploy_on: [team1, team2]
```

```python
# challenges/registry.py
REGISTRY = {
    "notes": NotesService,
    # "blog": BlogService,
}
```

**命名约定**：vulnerable services（攻防目标）放 `challenges/` 目录，CTF 圈惯用词；`orchestrator/service.py` 里的 `Service` ABC 保持不变（仍是更广义的抽象，将来可能覆盖控制平面 service 如 gateway / telemetry）。

## Service port & healthcheck

- **Port**：每个 Service 在 `game.yaml` 中声明自己的监听端口（不写死在框架里）。
- **`is_alive` 默认行为**：通用 `socket.connect((container_ip, service.port))` —— 端口接受连接即视为存活。
- **Per-adapter override**：需要协议级 healthcheck 的 Service 在子类里覆写 `is_alive`，例如 notes 用 `LIST → COUNT`，HTTP service 用 `GET /health` 或 `GET /`。
- M2 同步把 M1 `Service` ABC 的 `is_alive` 从 `@abstractmethod` 改成带默认 socket 实现的非抽象方法。

## Notes service（M2 首个具体 adapter）

参考 reference repo `src/adapters/game_simple/challenge_simple.py`：

- TCP server，命令 `PUT <text>` / `GET <id>` / `LIST`，监听 service 自己声明的端口（M2 默认 5000）
- **不用 systemd**：`install()` 通过 `docker cp` 把 server.py 放进容器；`start()` 用 `docker exec -d python3 server.py <port>` 后台启动
- `is_alive()` 覆写默认 socket 检查，加 `LIST` 命令验证响应 `COUNT `（确认是 notes 协议而非随便接受 TCP 连接的其它服务）

## Round 状态机

```
INIT -> PREFLIGHT -> SETUP -> DEFENSE <-> ATTACK -> STOPPED
                                ↑          ↓
                                └── round+=1
```

相位切换时机：

- 进入 DEFENSE：`orchestrator.network.set_cross_team_network(False)`（M2 no-op）+ 发 `PHASE_CHANGE` 事件
- 进入 ATTACK：`orchestrator.network.set_cross_team_network(True)`（M2 no-op）+ 发 `PHASE_CHANGE` 事件
- 每个 round 边界发 `ROUND_START` / `ROUND_STOP`

时长（M2 用 config 默认值，便于测试取短）：

```yaml
round_config:
  defense_default_secs: 10
  attack_default_secs: 10
  total_rounds: 3
```

## Event Bus

`asyncio.Queue` + 多订阅者（WS broadcast、内存 history、文件 sink）：

- `EventBus.publish(event)` 入队 + fan-out 给所有订阅者
- `EventBus.history()` 返回内存历史（GET /game/events 用）
- **`FileSink`**：每个 event 序列化成一行 JSON 追加到 `runs/<utc-ts>/events.jsonl`，GameMgr.start() 时建目录、open(append)，stop() 时 flush + close
  - 行格式：`{"ts": <epoch>, "type": "ROUND_START", "team_id": ..., "round_id": ..., "phase": ..., "payload": {...}}`
  - `runs/` 加入 `.gitignore`
- M2 实现的 EventType：`ROUND_START`、`PHASE_CHANGE`、`ROUND_STOP`、`SERVICE_DEPLOYED`

## 任务拆分

| # | 任务 | 输出 |
|---|------|------|
| 1 | `challenges/notes.py`（实现 Service ABC，无 systemd 版本） | 模块 |
| 2 | `challenges/registry.py` + `challenges/__init__.py` | 模块 |
| 3 | `config/game.yaml` 新增 `services:` 和 `round_config:` | YAML |
| 4 | `gamemgr/events.py`（`Event` dataclass + `EventType` enum + `EventBus` + `FileSink`） | 模块 |
| 5 | `gamemgr/round.py`（async 状态机 + 相位 timer） | 模块 |
| 6 | `gamemgr/game_mgr.py`（编排 Service deploy + Round Loop） | 模块 |
| 7 | `gamemgr/api.py`（FastAPI 路由 + WS） | 模块 |
| 8 | `main.py` 新增 `start` / `stop` 子命令（阻塞式运行 GameMgr + API） | CLI |
| 9 | `tests/test_m2_gameloop.py`（集成：setup + start → 至少跨一个相位 → 拉事件 → stop） | pytest |
| 10 | `requirements.txt`：`fastapi`、`uvicorn[standard]`、`httpx`（test 用） | 依赖 |

## 验证

```bash
./scripts/build_images.sh           # 已有，跳过
python main.py start &              # setup -> game.start -> uvicorn :8000
sleep 2

curl -s :8000/game/status           # {"phase": "DEFENSE", "round": 1, ...}

docker exec team1_dev bash -c \
  'echo LIST | nc -q1 team1_prod 5000' # COUNT 0   (notes adapter healthcheck via docker DNS)

sleep 12                            # 跨一次 DEFENSE -> ATTACK
curl -s :8000/game/events | jq      # 至少包含 ROUND_START + PHASE_CHANGE

curl -s -X POST :8000/game/stop     # 触发 stop -> teardown
wait                                 # main.py 退出

ls runs/                            # 期望：一个 UTC 时间戳目录
cat runs/*/events.jsonl | head      # 每行一个 JSON event

pytest tests/test_m2_gameloop.py    # 全绿
```

## 目录结构（M2 完成后）

```
CyberArenaOrchestration/
├── config/
│   ├── game.yaml                  # + challenges / round_config
│   └── docker/...
├── models/topology.py
├── orchestrator/                  # M1 不动（Service ABC 在此，is_alive 更新为带默认实现）
├── challenges/                    # vulnerable-service adapters
│   ├── __init__.py
│   ├── registry.py
│   └── notes.py
├── gamemgr/
│   ├── __init__.py
│   ├── config.py
│   ├── events.py
│   ├── round.py
│   ├── game_mgr.py
│   └── api.py
├── tests/
│   ├── test_m1_topology.py
│   └── test_m2_gameloop.py
├── main.py
├── requirements.txt
└── docs/
    ├── design_v1.md
    ├── m1_topology_plan.md
    ├── m1_topology_report.md
    └── m2_gameloop_plan.md
```

## 依赖新增

- `fastapi`
- `uvicorn[standard]`
- `httpx`（test 用）