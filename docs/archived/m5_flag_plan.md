# Milestone 5 — Flag Plant + Submit Works

**目标**：把 notes challenge 的 flag 机制接通端到端。游戏启动时给每个 (team, challenge) 植入一个 flag；`POST /game/flag/submit` 真实验证（不再是 stub 返回 `False`）；接受 / 拒绝都发 `FLAG_SUBMITTED` 事件。攻击方在 ATTACK 阶段能跨队 socket 把对手的 flag 偷出来、提交、拿到 `accepted=true`。

参考点：`../cyberarena/src/adapters/game_simple/challenge_simple.py` 的 `_put_flag` / `_get_flag` —— 同样的 notes 协议、同样的 PUT/LIST/GET 操作。**但 v1 的 gameserver_simple 整套 continuous-round + SLA + scoreboard 不照搬**，那些是 design_v1.md §5.2 显式列为 ⬜ stub 的（Poller / Healthcheck / Scoreboard），M5 不动它们。我们的 round 模型是 DEFENSE/ATTACK 双相位，flag 植入只在 game start 发生一次（design_v1.md §3.1 `plant_initial_flags`），不做 per-round rotation。

## 范围

✅ 实现：

- Flag 生成：`FLAG{<32 random A-Z0-9>}`，与 v1 同形（便于以后接 FAUST/v1 工具）
- `NotesService.plant_flag(container, flag)` —— PUT 一条 note，flag 即 payload；返回服务器分配的 index（信息性，验证不依赖它）
- `gamemgr/flag.py`：`FlagStore` —— 内存里 `{(team_id, service_name): flag_str}` + `submissions: set[(submitter_team_id, victim_team_id, service_name)]`
- `GameMgr.plant_initial_flags()` —— 遍历 challenges × deploy_on teams，每个 (team, service) 生成一个 flag、调对应 Service 的 `plant_flag`、写入 FlagStore；emit `FLAG_PLANTED` 事件（不带 flag 字符串，只带 team / service / round=0 / 时间戳）
- `GameMgr.submit_flag(submitter_team_id, flag) → SubmitResult` —— 真实验证四类返回：`ACCEPTED` / `UNKNOWN`（flag 不存在 / 已过期） / `OWN`（自己的 flag） / `DUPLICATE`（这一对受害者+service 已被同一队提交过）
- 提交后 emit `FLAG_SUBMITTED` 事件，payload `{accepted, reason, submitter_team_id, victim_team_id?, service?, phase}`。`flag` 字符串**不写进 event payload**（避免 panel / 日志泄露）
- `POST /game/flag/submit` body 接 `{team_id, flag}`，返回 `{accepted: bool, reason: "ACCEPTED"|"UNKNOWN"|"OWN"|"DUPLICATE"}`
- `main.py start` 在 `game.deploy_challenges()` 之后、`game.start()` 之前调 `game.plant_initial_flags()`
- 集成测试 end-to-end：plant → ATTACK 阶段在 team2_dev 容器里走 socket 把 team1 的 flag 拽出来 → `POST /game/flag/submit` 提交 → 断言 `accepted=true` + 事件流里有对应 `FLAG_SUBMITTED`

⬜ 不实现（推到 M5.5 或后续）：

- **Per-round flag rotation** —— 每个 DEFENSE 阶段开始重新植 flag。MVP 一局只一次植入；每个 (受害者, service) 最多被偷一次
- **Per-team submit token** —— 现在 body 里直接传 `team_id`（dev 接口）；v1 用 `X-Team-Token` header + per-team token 来鉴权，M5.5 抄过来
- **Scoring math / Scoreboard** —— `accepted` 计数留给 Scoreboard stub（design §5.2 ⬜），M5 只发事件、不维护分数
- **SLA / Poller / Healthcheck** —— design 一直列为 stub，M5 不动
- **Agent 主动攻击 smoke** —— 真让 Claude agent 完成"扫 prod → 拽 flag → 提交"流程是 M5.5 的事；M5 用一段裸 Python 代替 agent，验证机制
- **DEFENSE 阶段拒绝提交** —— 行为上 design 没要求；M5 任何相位都接受，事件 payload 里带 `phase` 让面板自己渲染颜色
- **Flag 过期/lifetime** —— 一局只一次植入，整局都有效

## 关键设计抉择（与 v1 的差异）

### 1. Plant 一次 vs continuous rounds

v1 (`game_simple._tick_loop`) 每个 round 给每队每 service 重植一个新 flag，旧 flag 在 `flag_lifetime_rounds` 后过期。这套机制配的是 FAUST 风格的连续 round + SLA 评分模型。

我们 v2 的 round loop 是 DEFENSE/ATTACK 双相位、固定轮数（`total_rounds`，默认 3）、相位间手动切换。design_v1.md §3.1 只画了 `plant_initial_flags` 一次。M5 严格照 design 做：**一次植入，整局有效**。重植入 = M5.5 增量。

### 2. 不抄 v1 的 HTTP 提交服务器

v1 的 `SimpleGameServer` 自带一个 `ThreadingHTTPServer` 监听 `submit_port`，用 `X-Team-Token` 验队伍身份。我们已经有 FastAPI 在 8000 端口，`POST /game/flag/submit` 走它。也不引入 token —— team_id 直接在 body 里。简单、和 design §5.3 一致。

### 3. 不抄 v1 的 `CheckerVerdict` / `SimpleChecker`

v1 把 `put_flag` / `get_flag` / `havoc` 抽成 `SimpleChecker` 三元组，喂给 gameserver 让它每轮调度。这套设施服务于"自动跑 SLA 检查 → 算分"的链路。我们 M5 不跑健康检查，所以 Service ABC 只加一个 `plant_flag` 方法就够了 —— `is_alive` 已经是 protocol-level 探测（M2 留下的）。

### 4. Flag 字符串不进 event payload

v1 event 里没有事件总线，但我们有。`FLAG_SUBMITTED` 事件会进 `runs/<ts>/events.jsonl`、走 WS 推到 panel、留在 history。Panel 是给观赛者看的，把 flag 明文丢上面会让攻击方"白嫖"——直接订阅就拿到所有 flag。所以 event payload 只带 `submitter` / `victim` / `service` / `accepted` / `reason`，flag 本身留在 FlagStore（内存）+ 服务器日志（access log 默认不抓 body）。

### 5. M5 不要求 agent 真攻击

M4 报告里把"agent → GameMgr 反向通道"推到 M4.5。M5 同步推："agent 完成攻击全流程"放 M5.5。M5 测试用一段裸 python（在 team2_dev 容器里 socket 连 team1_prod、走 LIST/GET、再 httpx 提交到 host），它扮演 agent 的角色，验证机制本身能跑通。这样 M5 / M4.5 / M5.5 解耦：M5 验证后端，M4.5 验证 agent 反向通道，M5.5 验证 agent 走完真正的攻击循环。

## 数据结构

```python
# gamemgr/flag.py

@dataclass(frozen=True)
class SubmitResult:
    accepted: bool
    reason: str   # ACCEPTED | UNKNOWN | OWN | DUPLICATE

@dataclass
class FlagStore:
    # (team_id, service_name) -> flag string
    planted: dict[tuple[str, str], str] = field(default_factory=dict)
    # 倒排索引：flag -> (owner_team_id, service_name)，submit 时 O(1) 查
    by_flag: dict[str, tuple[str, str]] = field(default_factory=dict)
    # 谁偷过谁的什么 service：(submitter, victim, service)
    submissions: set[tuple[str, str, str]] = field(default_factory=set)

    def plant(self, team_id: str, service: str, flag: str) -> None: ...
    def submit(self, submitter_team_id: str, flag: str) -> SubmitResult: ...
```

## API

```
POST /game/flag/submit
Content-Type: application/json
{
  "team_id": "team2",
  "flag": "FLAG{...}"
}

→ 200
{
  "accepted": true,
  "reason": "ACCEPTED"
}
```

## Service ABC 扩展

`orchestrator/service.py` 给 `Service` 加一个非抽象、有默认实现的方法：

```python
def plant_flag(self, container: ContainerHandle, flag: str) -> None:
    """Plant a flag onto this service. Default raises; concrete adapters override."""
    raise NotImplementedError(f"{self.name} does not implement plant_flag")
```

`NotesService.plant_flag` 直接 PUT，借用现成的 socket-from-inside-container 模式：

```python
def plant_flag(self, container, flag):
    probe = (
        f"import socket\n"
        f"s = socket.socket(); s.settimeout(3)\n"
        f"s.connect(('127.0.0.1', {self._port}))\n"
        f"s.sendall(b'PUT ' + {flag!r}.encode() + b'\\n')\n"
        f"print(s.recv(64).decode().strip())\n"
    )
    rc, out = self._client.containers.get(container.name).exec_run(
        ["python3", "-c", probe]
    ).exit_code, ...   # 期望 out.startswith("OK ")
```

## 任务拆分

| # | 任务 | 输出 |
|---|------|------|
| 1 | `gamemgr/flag.py`：`SubmitResult`、`FlagStore`、flag 生成函数 | 模块 |
| 2 | `gamemgr/events.py`：新增 `EventType.FLAG_PLANTED`、`FLAG_SUBMITTED` | 模块 |
| 3 | `orchestrator/service.py`：`Service.plant_flag` 默认实现（NotImplementedError） | 模块 |
| 4 | `challenges/notes.py`：`NotesService.plant_flag` 真实现 | 模块 |
| 5 | `gamemgr/game_mgr.py`：`plant_initial_flags()`、新版 `submit_flag()` | 模块 |
| 6 | `gamemgr/api.py`：`/game/flag/submit` 返回 `{accepted, reason}` | 模块 |
| 7 | `main.py`：`start` 子命令在 deploy_challenges 之后调 `plant_initial_flags()` | 模块 |
| 8 | `tests/test_m5_flag.py`：unit + integration | pytest |
| 9 | `docs/m5_flag_plan.md`（本文） / 后续 `m5_flag_report.md` | docs |

## 验证

### 单元

```python
# FlagStore
fs = FlagStore()
fs.plant("team1", "notes", "FLAG{abc}")
fs.plant("team2", "notes", "FLAG{xyz}")

assert fs.submit("team2", "FLAG{abc}").accepted is True
assert fs.submit("team2", "FLAG{abc}").reason == "DUPLICATE"
assert fs.submit("team1", "FLAG{abc}").reason == "OWN"
assert fs.submit("team2", "FLAG{nope}").reason == "UNKNOWN"
```

### 集成（端到端）

```bash
# 1. 起容器、装 challenge、植 flag
python main.py setup
python main.py start &
sleep 5

# 2. 从 team2_dev 跨队偷 team1 的 flag（ATTACK 阶段才行）
#    LIST -> COUNT 1 -> GET 0 -> "FLAG{...}"
docker exec team2_dev python3 -c "
import socket
s = socket.socket()
s.connect(('team1_prod', 5000))
s.sendall(b'GET 0\n')
print(s.recv(128).decode().strip())
"
# 期望：FLAG{...}（32 字符）

# 3. 提交
curl -s -X POST :8000/game/flag/submit \
  -H 'content-type: application/json' \
  -d '{"team_id":"team2","flag":"FLAG{...}"}'
# 期望：{"accepted":true,"reason":"ACCEPTED"}

# 4. 事件流里有这条
curl -s :8000/game/events | python3 -c "
import sys, json
events = json.load(sys.stdin)
for e in events:
    if e['type'] == 'FLAG_SUBMITTED':
        print(e['payload'])
"
# 期望：{'accepted': True, 'reason': 'ACCEPTED',
#        'submitter_team_id': 'team2', 'victim_team_id': 'team1',
#        'service': 'notes', 'phase': 'ATTACK'}
```

### pytest

```python
# tests/test_m5_flag.py

def test_flag_store_acceptance_classes():
    ...   # unit, 上面那四个 case

@pytest.mark.usefixtures("arena_started")
def test_plant_then_cross_team_steal_then_submit(client, http):
    """E2E: plant -> wait for ATTACK -> retrieve via socket -> POST submit."""
    # arena_started fixture: 已经 setup + deploy_challenges + plant_initial_flags
    # + 启动 RoundLoop（用 attack_secs=2, defense_secs=1, total_rounds=2 加速）

    # wait until phase == ATTACK
    wait_for_phase(http, "ATTACK", timeout=10)

    # team2_dev 跨队拿 flag
    flag = retrieve_flag_via_socket(client, "team2_dev", "team1_prod", 5000)
    assert flag.startswith("FLAG{") and flag.endswith("}")

    # submit
    r = http.post("/game/flag/submit", json={"team_id": "team2", "flag": flag})
    assert r.json() == {"accepted": True, "reason": "ACCEPTED"}

    # 事件流里能看到
    evs = http.get("/game/events").json()
    submitted = [e for e in evs if e["type"] == "FLAG_SUBMITTED"]
    assert len(submitted) == 1
    p = submitted[0]["payload"]
    assert p == {"accepted": True, "reason": "ACCEPTED",
                 "submitter_team_id": "team2", "victim_team_id": "team1",
                 "service": "notes", "phase": "ATTACK"}
    # flag 字符串绝不出现在事件里
    assert flag not in submitted[0]["payload"].values()

def test_submit_own_flag_rejected(client, http):
    """team1 提交自己 prod 上的 flag → OWN"""
    flag = retrieve_flag_via_socket(client, "team1_dev", "team1_prod", 5000)
    r = http.post("/game/flag/submit", json={"team_id": "team1", "flag": flag})
    assert r.json() == {"accepted": False, "reason": "OWN"}

def test_submit_unknown_flag_rejected(http):
    r = http.post("/game/flag/submit",
                  json={"team_id": "team2", "flag": "FLAG{NOTAREALFLAGNOTAREALFLAGNOTARE}"})
    assert r.json() == {"accepted": False, "reason": "UNKNOWN"}

def test_submit_duplicate_rejected(client, http):
    flag = retrieve_flag_via_socket(client, "team2_dev", "team1_prod", 5000)
    http.post("/game/flag/submit", json={"team_id": "team2", "flag": flag})  # 1st OK
    r = http.post("/game/flag/submit", json={"team_id": "team2", "flag": flag})  # 2nd
    assert r.json() == {"accepted": False, "reason": "DUPLICATE"}
```

通过判据：
- `pytest tests/test_m5_flag.py -v` 全绿
- `pytest tests/` 全绿（M1+M2+M3+M4+M5 = 24+ 个测试，不引入回归）
- M5.5 / M6 解锁条件成立：FlagStore.submissions 已经能拿来算分（Scoreboard 接入点）；事件流里 `FLAG_SUBMITTED` 已经在跑（panel 可消费）

## 安全注意

- **Flag 字符串不进 event payload** —— 已在范围里强调；面板订阅者 = 攻击方代理人，event 是公开广播
- **`team_id` 在 body 是 dev 接口** —— 真实 match 时换 `X-Team-Token`（M5.5）；现在 main 接口暴露在 `0.0.0.0:8000`，任何能 reach host 的人都能伪造 team_id。MVP 接受这风险
- **planted flag 在容器里是明文 in `STORE` 列表** —— 这是 challenge 本身的 vuln（notes 服务把所有 PUT 都暴露给 LIST/GET），就是 attack/defense 玩的点。**不要**在 challenge 里加权限校验"修复"它 —— 那是选手 / 防守 agent 该做的事
- **跨队 prod↔prod 只在 ATTACK 阶段开**（M3 已保证）—— DEFENSE 期间即便提交了正确 flag，攻击方也根本拿不到（socket connect 失败）；这层网络隔离不被 M5 改动

## 与后续 milestone 的对接

| Milestone | 依赖 M5 的什么 | M5 给 ta 留的口子 |
|-----------|----------------|--------------------|
| M4.5 (agent 反向通道) | 不依赖 M5 | —— |
| M5.5 (per-round rotation, per-team token, agent 完成攻击 smoke) | FlagStore + plant_initial_flags 模式 | `plant_initial_flags` 改名为 `plant_flags(round_id)` 在 RoundLoop 里挂；token 加到 game.yaml.teams |
| M6 (panel) | `FLAG_SUBMITTED` 事件流 | 已经在 EventBus 上发出，panel 订阅 `/events/stream` 即可 |
| Scoreboard（design §5.2 ⬜） | FlagStore.submissions 是数据源 | `scoreboard.get()` 直接 `len(submissions[team_id])` 起步 |
