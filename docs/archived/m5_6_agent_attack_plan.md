# Milestone 5.6 — Agent-Driven Attack Loop

**目标**：让 Claude agent 自己跑完 attack：从 dev 容器 SSH 进自己的 prod，从 prod 跨 `game_net` socket 拽对手的 flag，再 POST 提交。end-to-end live test 至少出现一次 `FLAG_SUBMITTED accepted=true reason=ACCEPTED`，submitter 是 LLM 而不是测试代码。

参考 design_v1.md §6.2 "agent controls its own ServiceContainer via SSH/root access from DevContainer"。M5.5 留下的 pivot 协议这个 milestone 来定：**SSH，root，per-team key，orchestrator 在 setup 时铺好**。

## 范围

✅ 实现：

- `config/docker/Dockerfile.prod`：装 `openssh-server`（已经有）；no entrypoint 改动，sshd 由 agent install 阶段按需启动
- **SSH provisioning 跟着 agent install 走，不在 Orchestrator 里**——把 pivot 当成 agent 自身的依赖，Orchestrator 不知道有 SSH 这件事：
  - `ClaudeAgentManager.install(dev_handle, cfg)` 里多一步：如果 `cfg.own_prod_container_name` 非空，
    1. 用 `ssh-keygen -t ed25519` 在 host 临时目录生成一对 key
    2. 通过 docker SDK 把 pubkey put 进 prod 的 `/root/.ssh/authorized_keys`，启 sshd
    3. 通过现有的 `_write_file` 把 privkey 写进 dev 的 `/root/.ssh/id_ed25519`（0600）+ `~/.ssh/config`（StrictHostKeyChecking=no）
    4. 把 SSH host 信息（own_prod + user + key path）一起写进 agent.env
  - 没指定 `own_prod_container_name` 的 AgentConfig（M4 hello-world、test override 等）走原路径，不动 SSH
  - 好处：Orchestrator 完全不知道 attack 模型存在；future codex / 其它 AgentManager 想用别的 pivot 协议自己改自己的 install()
- `models/agent.py`：`AgentConfig` 加这几个字段：
  - `team_id` — 自己的 team id
  - `team_token` — 写 X-Team-Token header 用
  - `own_prod_host` — SSH 目标（自己的 prod hostname / docker DNS 名）
  - `own_prod_container_name` — docker container 名；AgentManager 用它通过 docker SDK 注入 pubkey + 启 sshd
  - `opposing_prod_hosts: list[str]` — 攻击目标
  - `submit_url` — `http://host.docker.internal:8000/game/flag/submit`（dev 是 docker container，要靠 host gateway 寻 host）
- `gamemgr/game_mgr.py`：新增 `agent_configs(submit_base_url)` 方法，把 yaml 里 `agents[team_id]` 跟 `flags` / `topology` / `tokens` 合并出**每队**完整 `AgentConfig`（含 attack prompt）；caller（main.py 或 test）拿到后丢给 `AgentManager.install`
- `agents/claude.py`：现成支持，不改；只是 ClaudeAgentManager 写 agent.env 的循环加上新字段的 export
  - 或者更干净：AgentConfig 多一个 `extra_env: dict[str, str]` 字段，让上层注入任意 env，claude.py 只是循环写
- 新增 attack-mode prompt template（`agents/prompts.py`，简短的 Python 函数）：给定 (team_id, token, own_prod, opposing_prods, submit_url) 生成一段完整 prompt 文字
- `tests/test_m5_6_agent_attack.py`：env-gated 的 live integration test
  - skipif 没 real API key
  - setup 全套（包括 SSH 铺设）
  - install 一个攻击 agent（team2 攻 team1）
  - 轮询 `game.flags.submissions["team2"]` 出现 ≥1 个 entry 或 hits a hard timeout（默认 4 分钟）
  - 断言至少一次 ACCEPTED；attempts 数 / 耗时打到 pytest stdout 方便 debug

⬜ 不做：

- 两队同时跑（双向攻击）—— 增加 LLM cost、增加偶发性；single-direction 已经验证机制。双向是 M6 / scoreboard 之后的事
- Agent 自己识别该攻击谁 —— prompt 里直接喂列表
- Agent 攻击效率 / 多轮策略 —— 一次成功就 PASS；M5.6 不评分
- M4.5 反向通道（heartbeat / deploy_complete）—— agent 跑独立 loop，不需要 GameMgr 主动通知
- iptables / 网络层 audit —— SSH key 是 root，agent 在自己的 prod 里能做的事跟在 dev 里一样多；这是 design 接受的 trust model
- 多 agent flavor（codex / gpt） —— Claude only

## 关键设计抉择

### 为什么 SSH key + root, 不是 password / setuid 代理

- **key**：无交互，agent 直接 `ssh -i /root/.ssh/id_ed25519 root@own_prod`；不用回答 password prompt（claude 在 headless mode 没法）
- **root**：design §6.2 已经说 "agent controls its own ServiceContainer via SSH/root access"，trust model 就是这样
- **per-team**：team1 的 key 不在 team2 的 dev 里；team1_dev 的 agent 无论怎么折腾都 SSH 不进 team2_prod（key 不对 + 网络也不通）

### 为什么 ssh keys 在 host 临时生成 / 不烤进 image

- 烤进 image：所有部署用同一组 key，被复制粘贴的人就有 root access；CI 镜像扫描会标
- 烤进 dev image：dev/prod 各自 image，需要把 prod 的 pubkey 烤进 dev 的 image —— 编译时还不知道部署目标
- host 临时生成 + 运行时 inject：key 仅存在于本次 orchestrator 实例的生命周期，teardown 一起没；想换一局直接重新生成

代价：orchestrator setup 多了 ~1 秒（4 个容器 × 250ms 注入）。可以接受。

### dev 怎么知道 host 上的 gateway IP 用来 reach submit endpoint

dev 在 `team_net` 上，**不**在 host 默认 bridge 上。uvicorn 起在 host 的 `0.0.0.0:8000`，但从 dev 的角度看，host 是 `host.docker.internal`（Docker Desktop / OrbStack）或者 docker network 自己的 gateway（Linux daemon）。

两种解决：
1. `host.docker.internal` —— Docker Desktop 默认有，Ubuntu daemon 也有（`docker run --add-host=host.docker.internal:host-gateway` 或 docker-compose），但需要在 `Node.extra_hosts` 里配
2. 用 dev 所在的 docker network 的 gateway IP —— `team1_net` 的 gateway 是 `10.1.0.1`（subnet 第一个）

走 #1 更便携：给所有 dev 节点加 `extra_hosts: {host.docker.internal: host-gateway}`，在 `models.topology.Node` 已支持 env 但不支持 extra_hosts；加一个字段。

不过 #2 更直接：subnet 是 10.1.0.0/24，gateway 默认 .1。dev 配置时算一下，agent 拿到的 submit_url 是 `http://10.1.0.1:8000/game/flag/submit`。问题是这假设 docker 总把 gateway 放 .1（实际是）。

折中：加 `extra_hosts` 字段到 `Node`，给 dev 加 `host.docker.internal: host-gateway`；submit_url 用 `http://host.docker.internal:8000/...`。改动小，可移植。

### Attack prompt 设计

要素：
- agent 是 team N
- 自己的 prod 在哪、SSH 怎么进
- 对手 prod 列表
- 网络隔离规则（直接 reach 不到对手 prod，必须 SSH 经由自己 prod 中转）
- 攻击协议（LIST / GET / 找 FLAG{...} pattern）
- 提交协议（curl POST + X-Team-Token + JSON body）
- 一次成功 ≠ 任务结束，每隔 ~10s 重试（flag 会轮换）
- 时间预算：限制 turns 数 或 自然结束

Prompt 一段到两段，给具体命令例子。Claude 会自己用 Bash tool 跑。

### 测试设计

只验"机制走通"——agent 至少成功提交一次 flag。**不**验：
- 成功率 / 收敛速度
- 攻击轮数
- token 消耗

通过判据：`game.flags.submissions["team2"]` 出现至少一个 entry（说明 LLM 完成了 ssh→steal→submit 链）；retry 上限 4 分钟超时。

cost 估计：sonnet-4-6 ~10-30 cents / run。env-gated 跟 `test_live_hello_world` 一样默认 skip。

## 数据流

```
orchestrator.setup()      # unchanged — no SSH knowledge here
game.deploy_challenges()
game.plant_initial_flags()

# install agents (attack mode); SSH provisioning happens inside install()
for team in teams:
    agent_cfg = game.agent_config_for(team_id, submit_base_url=...)
    # AgentConfig carries: team_token, own_prod_host, own_prod_container_name,
    # opposing_prod_hosts, submit_url, system_prompt (attack template)
    ClaudeAgentManager().install(devN_handle, agent_cfg)
    #   └── if cfg.own_prod_container_name set:
    #       1. ssh-keygen → /tmp/cyberarena-keys-<sess>/teamN/
    #       2. put pubkey + start sshd on own prod
    #       3. put privkey + ssh config on dev
    #       4. write everything into agent.env so the LLM's bash sessions
    #          pick it up

game.start()              # round loop runs; agent autonomously SSH → steal → submit
```

## 任务拆分

| # | 任务 | 输出 |
|---|------|------|
| 1 | `models/topology.py`：`Node.extra_hosts: dict[str,str]` | 模块 |
| 2 | `orchestrator/infra/docker.py`：`create_and_start` 把 extra_hosts 传给 docker SDK | 模块 |
| 3 | `config/game.yaml`：dev nodes 加 `extra_hosts: {host.docker.internal: host-gateway}` | 配置 |
| 4 | `models/agent.py`：`AgentConfig` 加 `team_id` / `team_token` / `own_prod_host` / `own_prod_container_name` / `opposing_prod_hosts` / `submit_url` / `extra_env` | 模块 |
| 5 | `agents/claude.py`：install 阶段如果 cfg.own_prod_container_name 非空 → ssh-keygen + 注入 pubkey/启 sshd 进 prod + 注入 privkey/ssh config 进 dev；`agent.env` 写时 export 全部新字段 | 模块 |
| 6 | `agents/prompts.py`：新文件，`build_attack_prompt(cfg)` 返回 prompt 字符串 | 模块 |
| 7 | `gamemgr/game_mgr.py`：`agent_config_for(team_id, submit_base_url)` 合并所有信息构造完整 `AgentConfig` | 模块 |
| 8 | `tests/test_m5_6_agent_attack.py`：live test（skipif 无 key） | pytest |
| 9 | `docs/m5_6_agent_attack_plan.md`（本文）/ 后续 `m5_6_agent_attack_report.md` | docs |

## 验证

```bash
# 单元 + 之前所有的回归
$ pytest tests/

# live attack（消费 token）
$ pytest tests/test_m5_6_agent_attack.py::test_live_agent_attacks_and_submits -v -s
```

通过判据：
- 全套 tests 不回归
- live test：agent 跑完一段时间后 `game.flags.submissions["team2"]` 非空，且 `game.bus.history()` 里 ≥1 个 `FLAG_SUBMITTED accepted=true`

## 安全注意

- **agent.env 写 team_token 明文**：M5.5 已经 trust 这点（API key 也明文写在那）；同一台 dev 上的攻击者已经能读 token
- **per-team key**：team1 的 privkey 不在 team2 的 dev 里；保证两队 agent 互不能 SSH 进对方的 prod
- **sshd 接受密码**：`PasswordAuthentication no`（key-only），防止 agent / 选手把 root password 误配置出去
- **`StrictHostKeyChecking=no`**：dev 的 ssh config 关掉 host key 校验，避免第一次连接停下来问 yes/no。安全上：team_net 是私有，没有中间人；可以接受
- **agent 拿到自己 prod 的 root**：design §6.2 accept 这点。trust boundary 在 prod 容器边界，不在 prod 内部

## 与后续 milestone 的对接

| Milestone | 接 M5.6 的什么 |
|-----------|-----------------|
| Scoreboard | `game.flags.submissions` 持续被 agent 填，可以直接 sum |
| M6 panel | agent 提交事件已经在 bus 上跑；面板订阅 FLAG_SUBMITTED 渲染 |
| 多 agent flavor | `AgentManager` ABC 不变，AgentConfig 字段适用所有 flavor；codex 新写 OPENAI_* env，prompt 模板共用 |
| 攻击效率优化 | 不在这里做；observability + scoreboard 才能给出基线数据 |
