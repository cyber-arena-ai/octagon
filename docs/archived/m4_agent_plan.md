# Milestone 4 — Agent Install & Launch

**目标**：每个 dev 容器里安装并启动 LLM Agent CLI（默认 Claude Code）。API key 走 `.env`，`game.yaml` 只有 `${VAR}` 占位符。Agent 进程能被状态查询、能被干净 kill；orchestrator teardown 不留孤儿进程。

借鉴 reference repo 的 `src/adapters/agent_claude.py` 与 `config/test_smoke.yaml`，把 secrets 永远留在 shell env，yaml 入版本控制。

## 范围

✅ 实现：

- `.env`（gitignore）+ `.env.example`（入仓）放 API key 等敏感配置
- 配置加载阶段 `${VAR}` 占位符递归展开（ref repo `src/launch/main.py:_expand_env_vars` 模式），未设置时 fail-fast
- `game.yaml` 每队 `teams[i].agent:` 块：`cli` / `model` / `api_key` / `base_url`，敏感字段用 `${ANTHROPIC_API_KEY_TEAMn}` 占位
- Dockerfile.dev 加 `nodejs` + `npm`（Claude Code CLI 用），不烤 CLI 本身（保留每场升级灵活性）
- `agents/manager.py`：`AgentManager.install / kill / status / fetch_logs`，端口 ref repo `agent_claude.py` 的 supervisor-bash 模式（`setsid` 脱离 SSH 会话、独立 exit-code 文件）
- `agents/registry.py`：按 cli flavor 分发（`claude` → ClaudeAgentManager；后续 codex/gpt 加新 adapter）
- GameMgr 增加 `deploy_agents()` / `teardown_agents()` 钩子；`main.py start` 在 challenge deploy 之后调用
- `EventType` 新增 `AGENT_INSTALLED`（每队 install 完毕发一次）
- 集成测试用 `cli: sleep 60` override 替换 claude，避开 API key 依赖与 token 消耗，只验证安装 + 状态 + kill

⬜ 不实现（推到后续）：

- Agent → GameMgr WebSocket 反向通道（`heartbeat` / `deploy_complete`）— **M4.5**
- `last_deploy_time` 驱动 defense duration — M4.5
- 真实游戏 prompt（M4 只写占位 prompt） — M5
- `arena-deploy` 容器内 CLI 命令 — M4.5
- LLM gateway / 预算计量 — 远期

## .env 与占位符语义

```bash
# .env  (gitignored; NEVER commit)
ANTHROPIC_API_KEY_TEAM1=sk-ant-...
ANTHROPIC_API_KEY_TEAM2=sk-ant-...
ANTHROPIC_BASE_URL=https://api.anthropic.com
```

```bash
# .env.example  (committed; placeholder only)
ANTHROPIC_API_KEY_TEAM1=
ANTHROPIC_API_KEY_TEAM2=
ANTHROPIC_BASE_URL=https://api.anthropic.com
```

```yaml
# game.yaml
teams:
  - id: team1
    dev: team1_dev
    prod: team1_prod
    agent:
      cli: claude --dangerously-skip-permissions
      model: claude-sonnet-4-6
      api_key: ${ANTHROPIC_API_KEY_TEAM1}
      base_url: ${ANTHROPIC_BASE_URL}
```

加载流程：

1. `main.py` 启动时 `dotenv.load_dotenv()` 把 `.env` 注入 `os.environ`
2. `gamemgr/config.py:load()` 读 yaml 后调 `_expand_env_vars` 遍历 yaml 树（dict / list / str），对每个字符串里出现的 `${VAR}` 做替换
3. 任何 `${VAR}` 未设置 → `ValueError("env var X referenced in game.yaml but not set")`（fail-fast）
4. 展开后传给 GameMgr / AgentManager，下游代码看不到占位符

`_expand_env_vars` 复用 ref repo 的正则 `\$\{([A-Z_][A-Z0-9_]*)\}`；只支持大写蛇形（防小写 typo 误命中），不支持 `${VAR:-default}` 这种复杂语法。

**递归语义 + cycle protection**：

- "递归" **只**指遍历 yaml 树结构（dict/list/str 节点），不指对替换出来的字符串再扫一遍。
- 但替换出来的值仍可能含 `${...}`（比如某 env 值里又写了占位符）。为了同时支持"env 里嵌套引用"且避免无限循环，实现采用**有限轮迭代 + visited 集**：
  - 每轮把当前字符串里所有 `${VAR}` 替换为对应 env 值
  - 进入下一轮前，把这一轮触发的 var 名加进 visited
  - 若某轮替换出来的字符串里又出现已经在 visited 里的 var → `ValueError("cyclic env var reference: A -> B -> A")`，fail-fast
  - 也设 max depth（建议 8）兜底，防极端情况

例：

```
.env:  A=${B}   B=${A}
       → 展开 A 时第一轮变 ${B}，第二轮要展开 B，发现 A 已 visited → 报错
```

## Agent 安装时机与流程

```
orchestrator.setup()          M1
challenge deploy              M2
agent install      <—— M4
round loop start              M2 (+M3 网络切换)
...
teardown 时反向：agent kill → challenge stop → orchestrator teardown
```

`AgentManager.install(container, AgentConfig)`：

1. `mkdir -p /var/lib/cyberarena /var/log/cyberarena /var/run/cyberarena`
2. `npm install -g @anthropic-ai/claude-code` (idempotent；apt 装 nodejs/npm 已经在 dev-base 里)
3. 写 `/var/lib/cyberarena/prompt.txt`（M4 用占位 prompt：`"You are an A/D CTF agent. Wait for instructions."`）
4. 写 `/var/lib/cyberarena/agent.env`：
   ```
   export ANTHROPIC_API_KEY='...'
   export ANTHROPIC_BASE_URL='...'
   export IS_SANDBOX=1
   ```
5. supervisor-bash launch（参考 ref repo `agent_claude.py:install`）：
   ```bash
   setsid bash -c '
     set -a; . /var/lib/cyberarena/agent.env; set +a
     <cli> --model <model> --output-format stream-json --verbose \
       < /var/lib/cyberarena/prompt.txt \
       > /var/log/cyberarena/agent.stdout \
       2> /var/log/cyberarena/agent.stderr &
     echo $! > /var/run/cyberarena/agent.pid
     wait
     echo $? > /var/run/cyberarena/agent.exit_code
   ' </dev/null >/dev/null 2>&1 &
   ```
6. 短 sync barrier：等 pid 文件出现（最多 5s），未出现报错

`AgentManager.status(container)`：跑 ref repo 的 status probe（pid 文件存在 + `kill -0` → RUNNING；exit_code 文件存在 + 值映射 → COMPLETED/TERMINATED/CRASHED）。

`AgentManager.kill(container)`：**杀整个 process group，不只是父 PID**，idempotent。

Claude / Codex 这类 CLI 的进程树典型形态：

```
bash (supervisor)
 └── claude / codex (parent we track in agent.pid)
      └── node
           └── child helpers (http worker, fs watcher, …)
```

如果只 `kill -TERM <agent.pid>`，bash 包装层会先死、node 子进程会被 init reparent 成 orphan，留在容器里直到 container 停。所以 supervisor-bash 在 setsid 出来时既是新 session leader 也是新 process group leader，pgid 等于 wrapper 自己的 PID；install 阶段额外把 wrapper PID 写到 `/var/run/cyberarena/agent.wrapper.pid`：

```bash
# install supervisor bash (节选)
WRAPPER_PID=$BASHPID
echo $WRAPPER_PID > /var/run/cyberarena/agent.wrapper.pid
# ... 启动 claude 并记录 agent.pid 与 exit_code ...
```

`kill` 时：

```bash
WRAPPER_PID=$(cat /var/run/cyberarena/agent.wrapper.pid)
kill -TERM -- -$WRAPPER_PID 2>/dev/null || true   # 负号 = 进程组
```

负号语义触发 `killpg(2)`，bash + claude + node + 所有子孙一起收到 SIGTERM，没有孤儿。`agent.pid` 文件保留作为状态查询用途。

`AgentManager.fetch_logs(container)`：`cat` stdout/stderr/exit_code，组成 `AgentLogs`。

## 任务拆分

| # | 任务 | 输出 |
|---|------|------|
| 1 | `config/docker/Dockerfile.dev` 加 `nodejs npm`，重建 dev-base | Dockerfile + image |
| 2 | `requirements.txt` 加 `python-dotenv` | deps |
| 3 | `.env.example`（committed），`.gitignore` 加 `.env` | 配置 |
| 4 | `gamemgr/config.py`：load_dotenv + `_expand_env_vars` 递归展开 + agent 字段解析 | 模块 |
| 5 | `models/topology.py`（或新建 `models/agent.py`）：`AgentConfig` dataclass | 模块 |
| 6 | `game.yaml` 每个 team 加 `agent:` 块 | 配置 |
| 7 | `agents/__init__.py`、`agents/manager.py`（`AgentManager` ABC + status enum + logs dataclass） | 模块 |
| 8 | `agents/claude.py`：`ClaudeAgentManager` 具体实现（supervisor-bash） | 模块 |
| 9 | `agents/registry.py`：按 `cli` flavor 分发到 AgentManager 实现 | 模块 |
| 10 | `gamemgr/events.py`：新增 `EventType.AGENT_INSTALLED` | 模块 |
| 11 | `gamemgr/game_mgr.py`：`deploy_agents()` / `teardown_agents()`；在 `main.py start` 串起来 | 模块 |
| 12 | `tests/test_m4_agent.py`：用 `cli: sleep 60` override 验证 install/status/kill | pytest |
| 13 | `README.md`：加 `.env` 配置说明（macOS / Ubuntu 通用） | docs |

## 验证

```bash
# 1. 准备 .env（首次）
cp .env.example .env
# 编辑 .env 填入真实 API key（或保留空串跑非 LLM 测试）

# 2. 重建 dev-base（加了 nodejs+npm）
./scripts/build_images.sh

# 3. 跑游戏：现在会自动安装并拉起 agent
python3 main.py start &
sleep 30   # 给 agent install + claude 启动时间

# 4. 验证 agent 进程
docker exec team1_dev cat /var/run/cyberarena/agent.pid     # 期望：非空 pid
docker exec team1_dev ps -p $(docker exec team1_dev cat /var/run/cyberarena/agent.pid)
                                                              # 期望：进程存在

# 5. 验证 env 文件（API key 不应在镜像里，只在容器运行时）
docker exec team1_dev cat /var/lib/cyberarena/agent.env      # 期望：含 ANTHROPIC_API_KEY=sk-...

# 6. 拉事件
curl -s :8000/game/events | python3 -c \
  "import sys,json; [print(e['type']) for e in json.load(sys.stdin)]" \
  | grep AGENT_INSTALLED                                     # 期望：每队一行

# 7. 优雅停止：所有 agent 进程应被 kill
curl -s -X POST :8000/game/stop
wait
docker ps -a | grep team                                     # 期望：空

# 8. pytest（用 sleep 60 override，不消耗 API token）
pytest tests/test_m4_agent.py -v                             # 期望：全绿
pytest tests/                                                # 期望：M1+M2+M3+M4 全绿
```

## 安全注意

- **`.env` 永不入仓**：`.gitignore` + 文件名校验（CI 时跑 `grep -r ANTHROPIC_API_KEY <code>` 应为空）
- **API key 不进 image**：只在容器运行时通过 `docker exec` 写入 `/var/lib/cyberarena/agent.env`，image 层不含密钥
- **占位符未展开就 fail**：`game.yaml` 里 `api_key` 字段是 `${ANTHROPIC_API_KEY_TEAMn}`，若 .env 没设，启动直接报错（不会以空串发请求拿到 401 才发现）
- **错误日志不要 echo API key**：`AgentManager.fetch_logs` 默认不抓 `agent.env`；只取 stdout / stderr

## 目录结构（M4 完成后）

```
CyberArenaOrchestration/
├── .env.example                  # placeholder 模板（入仓）
├── .env                          # 真实 key（gitignore）
├── config/
│   ├── game.yaml                 # + agents 字段（${...} 占位）
│   └── docker/Dockerfile.dev     # + nodejs npm
├── agents/
│   ├── __init__.py
│   ├── manager.py                # AgentManager ABC + status / logs
│   ├── claude.py                 # ClaudeAgentManager
│   └── registry.py               # cli flavor -> AgentManager
├── gamemgr/
│   ├── config.py                 # + dotenv + ${VAR} expansion + AgentConfig
│   └── game_mgr.py               # + deploy_agents / teardown_agents
├── models/agent.py               # AgentConfig / AgentStatus / AgentLogs
└── tests/
    └── test_m4_agent.py
```

## 依赖新增

- `python-dotenv`（自动加载 `.env`）

## 推到 M4.5（Agent 反向通道）

`heartbeat` / `deploy_complete` 是设计 §6.2 的内容，但实现复杂度独立：

- Agent 容器内加一个 Python daemon 进程（与 LLM CLI 并行），WebSocket 长连接到 host
- GameMgr 加 WS endpoint 接 agent 连接，维护 `team_status` 映射
- `arena-deploy` 容器内 CLI 触发 `deploy_complete` 事件
- `RoundLoop` 用 `last_deploy_time` 计算 defense duration（`min(600s, max(across teams))`）

M4 把 agent 装上、跑起来、能干净停止；M4.5 让它能跟 GameMgr 对话。这样切分让 install 与 protocol 解耦，回归测试范围更可控。