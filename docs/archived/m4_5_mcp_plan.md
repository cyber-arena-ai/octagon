# Milestone 4.5 — MCP Agent Channel

**目标**：agent ↔ host 通信从 curl-REST 升级到 **MCP**。MCP server 挂在
现有 FastAPI 上（不另起进程），REST 保留作为兼容/调试通道。同时落地
v1.6 deferred 的 `AGENT_INSTALLED` 自动 emit 和 `DEPLOY_COMPLETE` 上报。

**定位**：MCP 是 **agent 的 structured action channel**（LLM → host
触发动作），**不是** lifecycle channel。Agent 生命周期由 host 驱动，三事件链：

| 事件 | 来源 | 触发点 | 可靠性 |
|------|------|--------|--------|
| `AGENT_INSTALLED` | host | install 准备段完成（文件 / env / mcp.json / ssh 全就位，**未** launch） | 确定性 |
| `AGENT_LAUNCHED` | host | `_launch` + `_wait_for_pid` 返回（supervisor 在跑，pid 拿到） | 确定性 |
| `AGENT_READY` | MCP `report_ready` tool | LLM 自己调 tool | best-effort |

**Agent-side WebSocket 彻底废弃**——v1 §6.2 的 WS daemon、heartbeat WS、
deploy_complete WS 全部移除。agent 反向通信**只有** MCP tool 一条路径，
不容许加第二条。这是硬规则，不是 deferred。
（注：`WS /events/stream` 是 panel 用，与 agent 无关——agent 既不连也不推。）

参考 [design_v1.md](design_v1.md) §6.2 / [design_v1.6.md](design_v1.6.md) §13。

---

## 范围

### Server side

新模块 `gamemgr/mcp_server.py`，基于 `mcp` Python SDK 的 `FastMCP`，
Streamable HTTP transport，挂到 FastAPI app 的 `/mcp` 前缀。

**URL 常量**（避免 mount-path 二处不一致；不写 auto-detect 代码）：

```python
# gamemgr/mcp_server.py
MCP_MOUNT_PATH      = "/mcp"   # FastAPI app.mount() 用
MCP_PUBLIC_ENDPOINT = "/mcp/"  # mcp.json / agent_config_for 用（尾斜杠避 307）
```

server 启动打印一行 `cyberarena MCP endpoint: http://<host>:<port>{MCP_PUBLIC_ENDPOINT}`。
如果 smoke test 报 SDK 实际在子路径下（`/messages` 之类），运维**手改**常量。

**MCP tools**（共三个，全在一个 server 里）：

| Tool | Args | 行为 |
|------|------|------|
| `submit_flag` | `flag: str` | 调 `GameMgr.submit_flag(team_id, flag)`，返回 `{accepted, reason}` |
| `report_ready` | — | emit `AGENT_READY` 事件（"LLM 在跑 + MCP 通"的 ack） |
| `report_deploy_complete` | `service: str, duration_ms: int \| None` | 校验 `game.is_known_service(service)`：通过则写 `deploy_reports[team][service] = {duration_ms, completed_at}` + emit `DEPLOY_COMPLETE`；不通过 raise `InvalidParams (-32602)`，**不写不发** |

每个 tool 从请求头 `X-Team-Token` 取 token → `game._token_to_team` 解析
team_id。未命中 → MCP error `{code: -32001, message: "Unauthorized"}`，
**不发任何事件**。message 故意通用（不含 `BAD_TOKEN` / `token` / team id
字串），避免变 token oracle。`BAD_TOKEN` 内部 reason 只写 host log。

**REST 双轨**：`POST /game/flag/submit` 不动，MCP `submit_flag` 共用
`GameMgr.submit_flag()` 函数体。`report_ready` / `report_deploy_complete`
**不**配 REST 备份（语义是"agent 自报"）。

**Host-side lifecycle emit**（`ClaudeAgentManager.install()` 内部）：
- 准备段（mkdir / 写文件 / ssh provision / npm install）结束
  → `on_installed(team_id)` → emit `AGENT_INSTALLED`
- `_launch` + `_wait_for_pid` 返回
  → `on_launched(team_id, wrapper_pid)` → emit `AGENT_LAUNCHED`
- 两个 callback 可选（默认 `None`），非-MCP 测试不变

### Agent side（dev container）

`ClaudeAgentManager.install()` 在 `cfg.mcp_endpoint` 非空时：

1. 写 `/var/lib/cyberarena/mcp.json`：
   ```json
   {
     "mcpServers": {
       "cyberarena": {
         "type": "http",
         "url": "<cfg.mcp_endpoint>",
         "headers": { "X-Team-Token": "<cfg.team_token>" }
       }
     }
   }
   ```
2. `chmod 600` 该文件（防容器内 non-root 读 token；同 `agent.env`）
3. `_launch` 命令追加 `--mcp-config /var/lib/cyberarena/mcp.json`

`agents/prompts.py` 改 attack prompt：curl 段换成
`mcp__cyberarena__submit_flag` tool 调用描述（Claude Code 给 MCP tool 自动
加 `mcp__<server>__` 前缀）。软鼓励 LLM 调 `mcp__cyberarena__report_ready`
但**不**当 load-bearing 信号。保留极简 curl fallback 一句。

### Config / 模型

- `models/agent.py`：`AgentConfig.mcp_endpoint: str = ""`。空 → 整个 MCP
  路径跳过；非空才写 mcp.json + 加 `--mcp-config`。**加性改动**，不破坏现有非-MCP 测试。
- `gamemgr/game_mgr.py`：
  - `deploy_reports: dict[str, dict[str, dict]]`，形如
    `deploy_reports[team][service] = {"duration_ms": int | None, "completed_at": float}`，覆盖写
  - `is_known_service(name) -> bool`：白名单来自 `challenges:` yaml
  - `record_agent_installed(team_id)` / `record_agent_launched(team_id, wrapper_pid)`
    / `record_agent_ready(team_id)` / `record_deploy_complete(team_id, service, duration_ms)`
    四个公开方法，对应四个事件的发射点
  - `agent_config_for()` 拼 `mcp_endpoint = f"{base_url}{MCP_PUBLIC_ENDPOINT}"`
    （从 `gamemgr.mcp_server` 导入常量，**不**手写字面量）
  - `submit_flag` 签名不变（所有 outcome 都 emit `FLAG_SUBMITTED` 的现状保持）
- `gamemgr/events.py`：加 `AGENT_LAUNCHED` / `AGENT_READY` / `DEPLOY_COMPLETE`
  三个枚举成员（`AGENT_INSTALLED` 已存在，本 milestone 语义收窄为"准备完成"）

### ⬜ 不做

- **adaptive defense duration**：`deploy_reports` 写入但 RoundLoop 不读，留给独立 milestone
- **`AGENT_CONNECTED` / `AGENT_DISCONNECTED`**：需要 session 跟踪，不做
- **heartbeat tool**：同上
- **MCP resources**（read-only 游戏状态查询）：减小 surface
- **多 CLI flavor**（codex / gpt）：来了再扩 `AgentManager` 子类
- **token 轮换 / 失效**：token 仍写死在 `game.yaml`
- **agent-side WebSocket（任何形式）**：硬规则，永久废弃，不是 deferred

---

## 关键契约

### submit_flag 事件矩阵

emit 点在 `GameMgr.submit_flag()` handler 内，**不**在 transport 层——
MCP/REST 共用一条事件流。`FLAG_SUBMITTED` payload 必含
`{submitter_team_id, flag, accepted, reason}`。

| 场景 | MCP response | REST response | emit? | event parity |
|------|-------------|---------------|-------|--------------|
| ACCEPTED / DUPLICATE / UNKNOWN / OWN | tool result `{accepted, reason}` | JSON `{accepted, reason}` | ✅ | payload 逐字段相同（除 ts） |
| BAD_TOKEN | error `code=-32001 "Unauthorized"` | HTTP 200 `{accepted: false, reason: "BAD_TOKEN"}` | ❌ 都不发 | parity-of-silence |

为什么 DUPLICATE / UNKNOWN / OWN 也 emit：scoreboard 想看 attempt 频率；
panel 每次 attempt 都该可见。BAD_TOKEN 单独静默以避免 token oracle。

**MCP 和 REST 的 BAD_TOKEN response shape 故意不同**——MCP 用通用
`Unauthorized` 防 oracle；REST 保留 M5.5 的 `BAD_TOKEN` reason
（curl 调试有用）。两边都不 emit 是唯一要 parity 的部分。

### service 名校验

`report_deploy_complete(service)` 进 handler 前必须查 `game.is_known_service()`。
未知 → InvalidParams，不写不发。白名单完全由 `game.yaml.challenges:` 决定，
**不绑定具体 service 名**——`Service ABC` plugin 模型不破。

理由：下游 adaptive duration 会 `max(duration_ms)` 聚合，typo 数据会污染。

### 三事件链 / 中段失败可观测

`install()` 内部两类操作的失败模式完全不同，分开 emit 才能区分：

| 阶段 | 操作 | 典型失败 |
|------|------|----------|
| 准备 | mkdir / 写文件 / ssh-keygen / npm install | 磁盘满 / 网络拉不到 npm / ssh 失败 |
| launch | `setsid bash` + `_wait_for_pid` | claude binary 缺 / CLI 立即 crash / pid 超时 |

混在一起 emit 一个 `AGENT_INSTALLED` → 缺事件只知道"挂了"，得读日志查
是哪步。拆开后 `INSTALLED` 出现但 `LAUNCHED` 缺失 = launch 段挂了。

Panel 主上线灯订阅 `AGENT_LAUNCHED`（信息量比 `INSTALLED` 大；和 INSTALLED
间隔毫秒级，用户无差别）。`AGENT_READY` 是补充诊断——LLM 在 headless 模式
可能漏调，不能作 load-bearing 信号。

### URL 路径陷阱

FastAPI `app.mount("/mcp", ...)` 触发 307 redirect 到 `/mcp/`，redirect
时 `X-Team-Token` header 可能被丢；FastMCP 子应用内部还可能再加路径
（SDK 版本相关）。

对策：两个常量分清楚（见上）+ Claude CLI smoke test 是 mount-path 兼容性的
**唯一硬证据**。不写 introspect SDK routes 的代码。

### Auth

token 写在 `mcp.json` 的 `headers.X-Team-Token` 字段，**不**作为 tool arg
传给 LLM（防 LLM 复述/输出泄露）。server 端共用 REST 的 `_token_to_team`。

### Streamable HTTP（不是 stdio / SSE）

跨容器场景：agent 在 dev container，server 在 host → stdio 不适用。
SSE 已被 MCP spec deprecate。Streamable HTTP 是当前推荐 + 完美匹配现有 FastAPI。

---

## 数据流

```
host:
  ClaudeAgentManager.install(dev, cfg, on_installed=..., on_launched=...)
    ├── prepare: mkdir, write mcp.json/agent.env/prompt, provision ssh, npm install
    │     └── on_installed(team_id)       → publish(AGENT_INSTALLED)
    └── _launch + _wait_for_pid
          └── on_launched(team_id, wrapper_pid)
                                           → publish(AGENT_LAUNCHED)

  uvicorn (FastAPI)
    ├── REST  /game/flag/submit  ──┐
    ├── REST  /game/...            │  shared GameMgr.submit_flag()
    ├── WS    /events/stream       │  (panel/observer ONLY — agent never connects)
    └── MCP   {MCP_MOUNT_PATH}        (clients hit {MCP_PUBLIC_ENDPOINT})
              ├── submit_flag       ──┘
              ├── report_ready        → publish(AGENT_READY)
              └── report_deploy_complete
                                      → is_known_service(svc)?
                                          publish(DEPLOY_COMPLETE)
                                        + deploy_reports[team][svc] = {duration_ms, completed_at}

dev container (per team):
  /var/lib/cyberarena/{prompt.txt, agent.env, mcp.json[chmod 600]}
  claude -p --mcp-config /var/lib/cyberarena/mcp.json < prompt.txt
    └── LLM call mcp__cyberarena__submit_flag(flag=...)
          └── HTTP POST http://host.docker.internal:8000{MCP_PUBLIC_ENDPOINT}
                       header: X-Team-Token: <token>
                       body  : JSON-RPC { method: "tools/call", ... }
```

---

## 任务

| # | 文件 | 任务要点 |
|---|------|---------|
| 1 | `requirements.txt` / `pyproject.toml` | pin `mcp==<specific version>`（不要 `>=`，SDK 还在演进） |
| 2 | `gamemgr/events.py` | 加 `AGENT_LAUNCHED` / `AGENT_READY` / `DEPLOY_COMPLETE` |
| 3 | `gamemgr/game_mgr.py` | 加 `deploy_reports` 字段、`is_known_service()`、四个 `record_*()` 方法；`submit_flag` 不动 |
| 4 | `gamemgr/mcp_server.py` (新) | `FastMCP("cyberarena")` 注册三个 tool；`build_mcp_app(game) -> ASGIApp`；token → team_id 解析；service 白名单校验；导出 `MCP_MOUNT_PATH` / `MCP_PUBLIC_ENDPOINT` 常量 |
| 5 | `gamemgr/api.py` | `app.mount(MCP_MOUNT_PATH, build_mcp_app(game))`；startup hook 打印 canonical URL |
| 6 | `models/agent.py` | `AgentConfig.mcp_endpoint: str = ""` |
| 7 | `gamemgr/game_mgr.py` | `agent_config_for()` 用 `MCP_PUBLIC_ENDPOINT` 拼 URL（禁止手写字面量） |
| 8 | `agents/claude.py` | `install()` 加 `on_installed` / `on_launched` 可选回调；`cfg.mcp_endpoint` 非空时写 `mcp.json`（含 token header、`chmod 600`）+ 加 `--mcp-config` |
| 9 | `agents/prompts.py` | attack prompt curl 段改成 MCP tool 调用；保留极简 curl fallback |
| 10 | `tests/test_m4_5_mcp_server.py` | Python SDK client 跑：三 tool 行为、`BAD_TOKEN` 通用化、submit_flag 4 valid outcomes MCP/REST event parity、unknown service 拒绝；最小 `httpx GET MCP_PUBLIC_ENDPOINT` 健康探测（不 404）；**不**复刻 JSON-RPC |
| 11 | `tests/test_m4_5_install_emits.py` | mock CLI；①happy：bus 出 `INSTALLED` 然后 `LAUNCHED`（顺序 + ts 递增）；②fault：mock `_launch` 抛错 → 只有 `INSTALLED` |
| 12 | `tests/test_m4_5_claude_mcp_smoke.py` (live, env-gated) | host 直接 `claude --mcp-config <file>` 调 `report_ready`，断言 bus 出 `AGENT_READY`。**唯一**真实 Claude CLI 兼容性硬证据 |
| 13 | `tests/test_m5_6_agent_attack.py` | 加断言：bus 出 `AGENT_INSTALLED` 和 `AGENT_LAUNCHED`（按顺序）；`AGENT_READY` / `DEPLOY_COMPLETE` 不作判据 |
| 14 | `docs/m4_5_mcp_plan.md` | 本文 + 后续 `m4_5_mcp_report.md` |
| 15 | `docs/design_v1.6.md` | 加 §14 MCP channel；更新 §5 事件表（加三个新 type、`AGENT_INSTALLED` 注解 emit 点）+ §13 deferred 表（划掉两项） |

---

## 验证

```bash
$ pytest tests/                                          # 全套回归
$ pytest tests/test_m4_5_mcp_server.py -v               # MCP server 单元
$ pytest tests/test_m4_5_install_emits.py -v            # 三事件链
$ pytest tests/test_m4_5_claude_mcp_smoke.py -v -s      # live CLI 兼容性，必跑
$ pytest tests/test_m5_6_agent_attack.py -v -s          # live attack
```

通过判据：

- 所有上面测试通过 + 现有测试不回归
- **Claude CLI smoke 必过**——这是 MCP 链路真实兼容性的唯一硬证据
- live attack 中 bus 必然按顺序出现 `AGENT_INSTALLED` → `AGENT_LAUNCHED`（host emit 确定性）；`AGENT_READY` / `DEPLOY_COMPLETE` 看 prompt 引导效果，不作判据

---

## 安全 / Rollback

- `mcp.json` 含 token 明文 + `chmod 600`（同 `agent.env` 的 trust model）
- bad token：MCP 一律 `Unauthorized` 通用错（不含 `BAD_TOKEN` / `token` / team id 字串）；REST 保留 M5.5 的 `{reason: "BAD_TOKEN"}`（asymmetry 是有意的）
- MCP server bind `0.0.0.0:8000`（同 REST，假定 host 网络 trusted）
- tool 输入校验：`submit_flag.flag` 长度 / 字符集限制；`duration_ms` 上界（防脏值）
- **回退到 REST-only**：`AgentConfig.mcp_endpoint` 留空 → 跳过 mcp.json + `--mcp-config`，agent 走 curl fallback
- **MCP server 启动失败**：uvicorn 整体失败，main.py 退出。**不**做 silent fallback（避免半 MCP 半 curl 的状态难调）
- **Claude CLI 不兼容**：smoke test 先暴露；真到生产配错，agent 退化成 "MCP tool unavailable" → 走 prompt 的 curl fallback

---

## 后续 milestone 对接

| Milestone | 用到 M4.5 的什么 |
|-----------|------------------|
| Adaptive defense duration | `deploy_reports[*][*]["duration_ms"]` 做 `min(600, max(...))`；空时 fallback `config.defense_default_secs` |
| Panel "connected dot" | `AGENT_LAUNCHED` 主灯（确定性）；`AGENT_READY` 加 ✓（best-effort）；`AGENT_INSTALLED` 渲染中间态 |
| Scoreboard | 用 `FlagStore.submissions`，与 M4.5 无直接关系 |
| 多 CLI flavor（codex / gpt） | `AgentManager` 子类各写各的 client config；server `/mcp` endpoint 不变 |
| `arena-deploy` in-container CLI | defense 部署完成后调 `mcp__cyberarena__report_deploy_complete` |
