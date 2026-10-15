# Milestone 6 — Observation Panel

**目标**：单文件 `panel/panel.html`，浏览器直接打开就能实时看 round / phase / 事件流。WS 连本地 GameMgr，零构建链路。验收以一次 live M5.6-style attack 期间的截图证明，写进 M6 report。

参考：
- `docs/design_v1.md` §7 (Observability Panel — ASCII mockup, event-type 表)
- v1 `vis/topology.py` 的视觉语言（暗色背景 + JetBrains Mono 字体 + 紫/黄/红/青色彩系统）
- 现有 `GameMgr.bus` + `gamemgr/api.py` 的 `WS /events/stream` + `GET /game/events`（M2 起就有）

## 范围

✅ 实现：

- `panel/panel.html` — 单文件，原生 JS（不引 D3 / React / 任何包），CDN 也不要。整套样式 inline 在 `<style>` 里
- 视觉：抄 v1 `vis/topology.py` 的色彩 / 字体（暗色 #0f0f1a 背景、JetBrains Mono / Inter、team 黄 / infra 青）
- 四块布局（mirror design §7.2 的 ASCII mockup）：
  1. **Header**：标题 + 当前 round / total + 当前 phase（带颜色：DEFENSE 蓝、ATTACK 红）
  2. **Timeline strip**：phase change 事件渲染成水平 band（一条 row，按时间从左到右）
  3. **Per-team rows**：每队一行，事件渲染成 chip：deploy / agent_install / flag_plant / flag_submit ✓ or ✗
  4. **Footer**：scoreboard placeholder + 连接状态（WS connected / reconnecting）
- 两路数据：
  - 开页时 `fetch('/game/events')` 拿历史（背景填充，避免空白屏）
  - 之后 `new WebSocket('/events/stream')` 接实时；断线自动重连（exponential backoff，max 30s）
- Event type → 视觉映射：

  | EventType | 渲染 |
  |-----------|------|
  | `ROUND_START` | header 的 round 数 +1 |
  | `PHASE_CHANGE` | header 的 phase 标签换色；timeline strip 加新 band |
  | `ROUND_STOP` | header 灰一下 |
  | `SERVICE_DEPLOYED` | 对应 team row 加 `deploy: <service>@<port>` chip |
  | `AGENT_INSTALLED` | footer agent status 改 connected |
  | `FLAG_PLANTED` | 对应 team row 加 🚩 chip（带 round 标） |
  | `FLAG_SUBMITTED accepted=true` | submitter row 加 绿 ✓ chip + payload reason |
  | `FLAG_SUBMITTED accepted=false` | submitter row 加 红 ✗ chip + reason（UNKNOWN/OWN/DUPLICATE） |

- 静态 `GET /panel` 路由（`gamemgr/api.py` 加）返回 panel.html 内容；浏览器开 `http://localhost:8000/panel` 即可。**不**用 `file://`，避免跨源烦恼
- Demo script `scripts/m6_screenshot_demo.py`：
  1. 起 arena + uvicorn
  2. plant_initial_flags
  3. 用 `google-chrome --headless` 截图 panel（空状态）
  4. install attack agent（real Claude，复用 M5.6 路径）
  5. 等 agent 完成（最多 4 分钟）
  6. 再截图 panel（攻击完成状态）
  7. 截图存 `docs/screenshots/m5_6_attack/{01_empty.png, 02_planted.png, 03_attack_done.png}`
- Smoke test `tests/test_m6_panel.py`：
  - panel.html 文件存在 + 含必要 element ID
  - `GET /panel` 返回 200 + 含 `Cyber Arena` 字符串
  - 用 `google-chrome --headless` 渲染 `/panel`，截屏，验证非空 PNG（>5KB）+ 含已知像素特征（暗色背景）

⬜ 不做：

- **Service health 指示器** — 依赖 Poller，design §5.2 一直是 ⬜ stub
- **Agent online/offline 指示器** — 需要 M4.5 反向通道（heartbeat）；M6 只能看 `AGENT_INSTALLED` 这种一次性事件，installed=connected 是粗糙近似，不做误导性绿点
- **Scoreboard 表格** — Scoreboard milestone 的事
- **Multi-client WS coordination / auth** — MVP 一次只有一个观赛者，不做
- **File-based replay** — runs/<ts>/events.jsonl 已经在落盘，replay tool 独立写
- **D3 / React / time-axis math** — vanilla 够了，未来要 timeline 缩放再说

## 关键设计抉择

### 单文件 + 服务端 inline，不用 CDN

design §7 说 "WS 地址通过 URL 参数配置 (?ws=ws://remote:8000/events/stream)，后续无需改代码即可远程接入" — 保留这个能力（panel.html 读 URL `?ws=`，没有就走相对路径）。但 D3 / Tailwind / 任何外部 JS 都不引：
- demo / 离线开发场景 panel 应该开就能用
- panel.html 进版本控制，跟着 commit 走，不依赖网络
- 加 D3 之类的成本是再装一层心智负担，目前没复杂可视化需求

### 走 `GET /panel` 而非 `file://`

WS endpoint 在 `:8000`。如果 panel 用 `file://` 打开，浏览器把它视为 origin `null`，连 ws://localhost:8000 时 CORS / mixed-content 规则各异；最方便的方案是 panel 跟 WS 同源——uvicorn 给一条 `/panel` 静态路由。代码量 5 行，省一堆边缘情况。

### Vanilla JS，不上框架

整个面板就 4 块、一两百行 JS 撑死。React / Vue / Svelte 全是 build step，build step = 多一层在 panel 加载失败时怀疑对象。原生 + 一个 `<script>` 块。

### 视觉抄 v1 vis/topology.py 的色彩 / 排版

v1 的 vis 是为同样这个产品做的；视觉一致性帮以后整合（如果哪天合并工具链）。颜色照搬：
- bg `#0f0f1a` / text `#e0e0e0`
- accent purple `#7c6ef0`（标题、active phase）
- team yellow `#ffe66d`
- attack red `#ff6b6b`
- defense / infra teal `#4ecdc4`
- font Inter + JetBrains Mono

### Screenshot 用 `google-chrome --headless`，不引 playwright

`/usr/bin/google-chrome` 已经在 host 上；`--headless --screenshot=path.png --window-size=W,H http://...` 一行命令出图。playwright 多 ~100MB chromium 下载 + 一层 API 抽象，M6 只需要"截 3 张图"，过度装备。

### 截图存仓库（不 gitignore）

`docs/screenshots/m5_6_attack/*.png`——M5.6 / M6 的报告引用这些图，需要常驻 repo。文件不大（每张 PNG ~50-200 KB），版本控制可以接受。

## 数据流

```
browser opens http://localhost:8000/panel
   │
   ├─ GET /panel  →  serves panel/panel.html  (FastAPI FileResponse)
   │
   └─ panel JS:
        ├─ fetch('/game/events')  →  hydrate historical events
        └─ new WebSocket('ws://.../events/stream')
              ├─ on message → render(event)  (mutate DOM in place)
              └─ on close → reconnect with backoff
```

## API 改动

```python
# gamemgr/api.py
from fastapi.responses import FileResponse

@app.get("/panel")
async def panel_page():
    return FileResponse(PANEL_HTML_PATH, media_type="text/html")
```

无 schema 改动，无 event 改动；M6 全是消费侧。

## 任务拆分

| # | 任务 | 输出 |
|---|------|------|
| 1 | `panel/panel.html` — layout + CSS（v1 配色）+ event-render JS | 文件 |
| 2 | `gamemgr/api.py` — `GET /panel` route | 模块 |
| 3 | `tests/test_m6_panel.py` — smoke (file existence + chrome render + non-empty PNG) | pytest |
| 4 | `scripts/m6_screenshot_demo.py` — orchestrated live demo + screenshot capture | 脚本 |
| 5 | `docs/screenshots/m5_6_attack/{01,02,03}.png` | artifact |
| 6 | `docs/m6_panel_plan.md`（本文）/ 后续 `m6_panel_report.md`（带截图） | docs |

## 验证

### Smoke

```bash
$ pytest tests/test_m6_panel.py -v
# 期望：panel.html 存在 + chrome 能 render 出非空 PNG
```

### Live demo + screenshots

```bash
$ python scripts/m6_screenshot_demo.py
# 顺序：setup → deploy → plant → 截图1（base state） → install agent → 等  
#        → 截图2（attack done）
# 输出：docs/screenshots/m5_6_attack/*.png
```

通过判据：
- `pytest tests/` 全套不回归（仍是 42 + 新增 smoke）
- `docs/screenshots/m5_6_attack/` 至少 2 张 PNG，肉眼看得到 ROUND/PHASE header + 至少一条 `FLAG_SUBMITTED ✓`

## 安全注意

- panel 是只读的；没有改 state 的 API 暴露。任何能 reach `:8000` 的人都能看 panel
- panel 接 `/events/stream`——M5 / M5.5 已经保证 flag 字符串不在 event payload；panel 渲染的 chip 也只用 `submitter` / `victim` / `service` / `accepted` / `reason`
- BAD_TOKEN 不进事件流（M5.5 设计）——panel 自然就看不到 token 探测尝试，跟期望一致

## 与后续 milestone 的对接

| Milestone | 接 M6 的什么 |
|-----------|---------------|
| Scoreboard | 加一个 ScoreSummary section 渲染 `/game/scoreboard`（M5.5 已经把 submissions 数据准备好了） |
| 服务健康 / Poller | 加 service status badge per team row；事件流多 SERVICE_UP / DOWN 即可消费 |
| M4.5 reverse channel | 真正的 agent connected / disconnected 灯（heartbeat 超时 → red） |
| File-based replay | 把 fetch + WS 部分替换成 fetch jsonl + 模拟时间播放器 |
