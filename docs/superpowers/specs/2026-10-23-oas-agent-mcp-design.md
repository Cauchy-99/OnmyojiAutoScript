# OAS Agent MCP 设计

- 日期：2026-10-23
- 状态：待评审
- 仓库：`D:\dev\OnmyojiAutoScript`（OnmyojiAutoScript，GPL-3.0）
- 分支：`feat/agent-mcp`（不直接进 `master`，官方升级冲突后续再处理）

## 1. 背景与目标

现在：OAS 后端（FastAPI，`server.py` → `uvicorn`，默认 22267）提供 HTTP/WebSocket 接口，OASX（Flutter）作为 GUI，由人手动选任务。OAS 自带调度器负责「按 next_run 挑下一个任务并执行」。

目标：把 OAS 的能力通过 MCP 暴露出来，让 agent 每天自动编排、执行、纠错；人只在两类时机介入——不可逆/花钱的动作，以及「朋友约了一起玩」时的让位。

明确非目标：

- 不重写 OAS 的任务实现（图像识别、战斗逻辑、各 `tasks/*/script_task.py`）。
- 不做新 GUI，不改 OASX。
- 不做多机/多账号跨机调度（单机多实例 `oas1/oas2` 支持，但不做分布式）。
- 不让刷任务的 agent 改源码。

## 2. 现状（代码事实）

| 事实 | 位置 |
|---|---|
| Web 服务入口：`uvicorn.run("module.server.app:fastapi_app")`，端口取 `deploy.yaml` 的 `WebuiPort`（默认 22267） | `server.py:86` |
| 全局单例 `mm = MainManager()`，持有 `script_process: dict[name, ScriptProcess]` | `module/server/main_manager.py:114` |
| 每个实例是一个 `multiprocessing.Process`，子进程里跑 `Script(config).loop()` | `module/server/script_process.py:43` |
| `Device`（ADB/DroidCast/scrcpy/窗口）**只存在于子进程** | `script.py:80-91` |
| 配置是磁盘上的 `config/<name>.json`，用 pydantic 模型映射；`ConfigModel.__setattr__` 会立即 `save()` | `module/config/config_model.py:170-179` |
| 子进程在任务边界 `del_cached_property(self, 'config')` 重新读盘 | `script.py:664,732,736` |
| 调度器：`update_scheduler()` 按 `scheduler.next_run` 分 pending/waiting，支持 Filter/FIFO/Priority | `module/config/config.py:177-242`、`module/config/scheduler.py` |
| 任务开关就是 `scheduler.enable`，下次运行时间就是 `scheduler.next_run`（`"YYYY-MM-DD HH:MM:SS"`） | `config/template.json` |
| 配置的写接口已存在：`script_task()` / `script_set_arg()` | `module/config/config_model.py:295,361` |
| 日志：磁盘 `./log/<date>_<name>.txt`，同时子进程通过 `multiprocessing.Pipe` 推给 Web 进程广播 | `module/logger.py:132-165` |
| 「重启游戏」的既定做法是给 `Restart` 任务排队：`config.task_call('Restart')` | `script.py:573,582` |
| `GotoMain` 任务模块存在，`self.run('GotoMain')` 是回主界面的既定做法 | `tasks/GotoMain/script_task.py`、`script.py:515` |

关键推论：**配置文件是唯一可靠的跨进程媒介**，设备操作只能在子进程里做。

## 3. 架构

### 3.1 进程模型

```
DSH (agent)  ── streamable-http ──▶  Web/API 进程 (uvicorn :22267)
                                      ├─ FastAPI：现有 routers + /api/agent/* + /mcp
                                      ├─ mm : MainManager（实例状态、进程句柄）
                                      └─ bridge：命令队列 / 帧缓存的父进程侧
                                            │  multiprocessing.Queue
                                            ▼
                                     脚本子进程 ×N
                                      ├─ Script.loop()  ← 唯一持有 Device 的线程
                                      └─ Config（任务边界重新读盘）
```

### 3.2 三条硬约束

1. **`Device` 只在子进程。** 截图、点按、启停游戏都必须在子进程里做，否则会和正在跑的任务抢 ADB / DroidCast / scrcpy 通道。不使用 zerorpc：旧 Qt GUI 的子进程只跑 RPC 服务、不跑 `loop()`，而这里子进程在跑 `loop()`，跨线程调 `Device` 会污染 `click_record`/`stuck_record`/`screenshot_deque`，可能触发假卡死。
2. **配置改动在任务边界生效。** 写 `config/<name>.json` 不会打断正在跑的任务（子进程下一个边界重读）。想「立刻」打断，只有 `ScriptProcess.stop()`（terminate，等价 GUI 点停止）。
3. **单次读写有锁，读—改—写整段没有。** `read_file`/`write_file` 各自持 `FileLock(<file>.lock)` 并用 `atomic_write` 落盘（`module/config/utils.py:33-101`），所以单次操作不会写坏文件；但「读到旧值 → 改 → 写回」之间的窗口没有跨进程互斥，父子进程并发仍可能丢更新。能力层采用「读—改—立即写—回读校验」，不一致时重试一次并记录到 journal。

### 3.3 两条新通道（都不跨线程碰 Device）

- **命令队列** `cmd_queue: multiprocessing.Queue`，父 → 子。子进程在**任务边界**、`wait_until()` 的 5 秒轮询、`_try_acquire_queue_token()` 的 30 秒轮询里消费：
  `goto_main` / `restart_game` / `stop_game` / `emulator_start` / `emulator_stop`。
  `restart_game` 复用 `config.task_call('Restart')`，不直接调设备。
- **帧缓存** `frame_queue: multiprocessing.Queue(maxsize=1)`，子 → 父。子进程起一个 daemon 线程，每 N 秒（默认 5，可配 0 关闭）读 `self.device.image`（**只读引用，不调 `screenshot()`**），缩放到最长边 1280 后 JPEG 编码，先清空再 `put`。父进程后台协程按实例名抽干队列、各存最新一帧。MCP 的 `screenshot(instance)` 返回该实例那帧，最多 N 秒旧，零干扰；多实例互不串台。

### 3.4 目录结构

```
module/server/agent/
  __init__.py
  models.py      # 数据契约（dataclass / pydantic）
  snapshot.py    # 只读：实例、队列、任务、参数
  ops.py         # 写：开关、参数、next_run、补跑、插队、启停、设备命令
  policy.py      # 动作分级、hold、protected_tasks 判定
  logtail.py     # 增量读日志文件
  memory.py      # data/agent 下的 state / journal / issues
  bridge.py      # 命令队列与帧缓存的父进程侧
module/server/agent_router.py   # REST /api/agent/*
module/server/mcp_server.py     # FastMCP 工具（薄壳，调用上两层）
module/server/app.py            # 挂载 router + /mcp（含 lifespan 接线）
script.py                       # +: 消费命令队列、发布帧
module/server/script_process.py # +: 建队列、传参、暴露 bridge
docs/agent/daily-playbook.md    # headless 的巡检提示词来源
tests/agent/                    # 新增测试
data/agent/                     # 运行期状态（gitignore）
  workspace/                    # 刷任务会话的工作目录（沙箱边界）
  state.json
  daily_plan.yaml
  journal/YYYY-MM-DD.md
  issues/
  approvals.json
```

## 4. 能力层

业务函数写一遍（`snapshot.py` / `ops.py`），REST 和 MCP 只是两个壳。所有工具**非阻塞**：不在工具内部等任务跑完（MCP 单次调用默认 60 秒超时），进度靠 `get_snapshot` + `get_log` 轮询。

### 4.1 只读

| REST | MCP 工具 | 返回 |
|---|---|---|
| `GET /api/agent/briefing` | `get_briefing` | 一次给全：实例列表 + hold 状态 + `daily_plan.yaml` + 今日 `state.json` + playbook 摘要。设计上让第一次调用足够决策。 |
| `GET /api/agent/instances` | `list_instances` | `[{name, state, pid_alive, running_task, frame_age_s, hold}]` |
| `GET /api/agent/{name}/snapshot` | `get_snapshot` | `state`、`running_task`、`schedule{running,pending,waiting}` |
| `GET /api/agent/{name}/tasks` | `list_tasks` | `[{task, enable, next_run, priority, success_interval, failure_interval}]` |
| `GET /api/agent/{name}/tasks/{task}/args` | `get_task_args` | 参数 schema + 当前值（复用 `gui_args`） |
| `GET /api/agent/{name}/log?cursor=&limit=` | `get_log` | `{lines, cursor, eof}`；游标为字节偏移，跨轮次可续 |
| `GET /api/agent/{name}/screenshot` | `screenshot` | 最新帧（MCP 里是 image content；REST 里是 `image/jpeg`） |

### 4.2 写

| REST | MCP 工具 | 语义 | 生效 |
|---|---|---|---|
| `POST /{name}/running` `{running: bool}` | `set_instance_running` | 启/停子进程（停机等价 GUI 停止，是唯一即时中断） | 立即 |
| `POST /{name}/command` `{command}` | `send_device_command` | `goto_main`/`restart_game`/`stop_game`/`emulator_start`/`emulator_stop` | 下一任务边界 |
| `POST /{name}/tasks/{task}/enable` `{enabled}` | `set_task_enabled` | 改 `scheduler.enable` | 下一任务边界 |
| `POST /{name}/tasks/{task}/args` `{path, value, value_type}` | `set_task_args` | 改 `group.arg`，复用 `script_set_arg` | 下一任务边界 |
| `POST /{name}/tasks/{task}/next_run` `{at}` | `set_task_next_run` | 改 `scheduler.next_run` | 下一任务边界 |
| `POST /{name}/tasks/{task}/run_now` `{position}` | `run_task_now` | `front`=现在，`end`=now+5min；确保实例在跑 | 下一任务边界 |
| `POST /{name}/schedule_rule` `{rule}` | `set_schedule_rule` | `Filter`/`FIFO`/`Priority` | 下一任务边界 |
| `POST /api/agent/emergency_stop` | `emergency_stop` | 停所有实例 + 进 hold | 立即 |
| `POST /api/agent/hold` `{minutes}` / `DELETE` | `hold_device` / `release_device` | 让位闸门 | 立即 |
| `POST /api/agent/daily_state` `{patch}` | `update_daily_state` | 合并写 `state.json` | 立即 |
| `POST /api/agent/journal` `{text}` | `log_decision` | 追加当日 journal | 立即 |
| `POST /api/agent/issues` | `file_issue` | 落 issue（见 §6） | 立即 |
| `GET /api/agent/issues` | `list_issues` | 列出 issue | — |

`run_task_now` 的实现：`config.task_call(task)`（内部把 `next_run` 置为 now 并保存）→ 若实例未运行则 `ScriptProcess.start()` → 回读校验。`front` 与 `end` 的差别只在 `next_run` 取 now 还是 now+5min：调度器按 `next_run` 升序取 pending，所以 now 天然插到队首。

### 4.3 生效时机与一致性（对 agent 的承诺）

- 读工具永远读磁盘最新值（每次新建 `Config(name)`），不缓存。
- 写工具返回 `{ok, applied_at, effective: "next_task_boundary"}`，并回读校验一次。
- 正在跑的任务不会因为写配置而中断——这一点必须写进工具描述，否则 agent 会误以为「改了参数立刻生效」。

## 5. 每日闭环

### 5.1 `data/agent/daily_plan.yaml`（人写的意图，不是配置副本）

```yaml
timezone: Asia/Shanghai
wake: ["07:00", "12:00", "18:00", "22:30"]
must: [Exploration, WantedQuests, SoulsTidy]     # 当天没做完就插队
optional: [KittyShop, MysteryShop]
protected_tasks: [MysteryShop, RichMan, TalismanPass]  # 触碰需人工批准
budget: {max_active_hours: 10, quiet_hours: ["23:30-07:00"]}
on_failure: {retry: 2, then: Restart, then: skip, notify: true}
```

### 5.2 `data/agent/state.json`（agent 的当日记忆）

```json
{"date":"2026-10-23","instance":"oas1","done":["Restart","WantedQuests"],
 "skipped":["Orochi"],"attempts":{"Orochi":2},"last_wakeup":"2026-10-23T18:00:12+08:00",
 "notes":"御魂失败两次，已落 issue ISS-20261023-181500-orochi"}
```

headless 每次是新会话，靠这个文件接上；常驻会话也读它，避免重复决策。

### 5.3 `journal/YYYY-MM-DD.md`

每次决策一行：`HH:MM 动作 → 理由 → 结果`。出问题时人（或换的强模型）能回看「它为什么这么排」。

### 5.4 两种拉起方式，共用同一份状态

- **常驻**：DSH 会话里 `schedule_create(every 15min)` 自我唤醒巡检。有上下文，判断连续。
- **headless**：Windows 任务计划在 `wake` 时刻跑 `dsh --profile headless "<prompt>"`，prompt 要求先 `get_briefing`、按 playbook 巡检、干完退出。不依赖常驻进程，崩了下一轮自愈。

两种模式都只做「现在该做什么」，不假设自己记得上一轮。

### 5.5 单轮循环

```
get_briefing → 读 plan + state + 当前队列
  → 若 hold 生效：只读，结束
  → 选动作（补齐 must / 纠错 / 什么都不做）
  → 写（每次只写一件事，回读校验）
  → get_snapshot / get_log 确认
  → update_daily_state + log_decision
  → 还有事就继续，没事就退出本轮
```

### 5.6 失败与跳过（「刷不过就跳过」）

单个任务失败 2 次（`on_failure.retry`）后：标记当日 `skipped`，落 issue，继续下一个。同一实例连续 3 个不同任务失败：停止刷、进 hold、落 issue 并要求人工。绝不因为一个任务卡住而反复重试烧时间。

## 6. 运行期沙箱与 issue 通道

刷任务的模型可能很弱，所以限制不靠提示词，靠**物理边界**。

### 6.1 三重限制

1. **工作目录沙箱**：刷任务会话的 cwd 固定为 `data/agent/workspace/`。DSH 的文件沙箱按工作目录授权，写 `module/`、`tasks/` 会被拒绝——它在物理上改不了源码。这是主边界。
2. **MCP 不提供任意写**：工具面里没有任何「写文件/执行命令/改源码」的接口。所有状态写入由 MCP 服务进程自己完成（`data/agent/` 下），agent 只能通过有语义的工具表达意图。
3. **失败即跳过**：见 §5.6。弱模型的典型失败模式是「同一个任务反复重试到超时」，这条直接掐掉。

加固项（可选）：给刷任务单独一个 DSH profile，deny 掉 bash / 写文件类工具。属于实施阶段的加固，不作为正确性的前提。

### 6.2 issue 通道（给未来的强模型）

目录 `data/agent/issues/`，一条一个文件：`ISS-YYYYMMDD-HHMMSS-<task>.md`。

```markdown
---
id: ISS-20261023-181500-orochi
created_at: 2026-10-23T18:15:00+08:00
instance: oas1
task: Orochi
phase: run
failures: 2
status: open
---
## 症状
任务连续失败 2 次，日志末尾见异常。
## 当时的快照
- state: RUNNING, running_task: Orochi
- 队列: pending=[...], waiting=[...]
## 日志片段
```
<最后 80 行，去重去噪>
```
## 截图
data/agent/issues/ISS-20261023-181500-orochi.jpg（若有帧）
## 复现建议
<agent 的一句话猜测，允许为空>
```

工具：`file_issue(task, symptom, log_excerpt?, suggestion?)` → 服务进程自己抓快照、抓当前帧、写文件；`list_issues(status?)`。人之后用强模型复现处理；修完把 `status` 改 `resolved`。

设计意图：issue 是**单向通知**，不是让弱 agent 自己修。它只负责「留下足够的证据 + 不添乱」。

## 7. 安全与授权

### 7.1 只监听回环

`/api/agent/*` 与 `/mcp` 加中间件：`request.client.host` 不是 `127.0.0.1`/`::1` 时返回 403。OAS 默认 bind `0.0.0.0`，没有这层等于把游戏交给整个局域网。若将来要远程，再引入 token（`server.py` 已有 `-k/--key` 参数位）。

### 7.2 动作分级（`policy.py`）

| 级别 | 例子 | 处理 |
|---|---|---|
| read | 所有只读工具 | 放行 |
| reversible | 开关任务、改 next_run/参数、补跑、启停实例、设备命令 | 放行；写前写后各存一份快照进 journal，可回滚 |
| irreversible | `protected_tasks` 里任务的开关与参数变更、任何急停之外的全局动作 | 拒绝，落 `approvals.json` 待人工批准；headless 下永不自动通过 |

`protected_tasks` 由 `daily_plan.yaml` 配置，默认 `[MysteryShop, RichMan, TalismanPass]`（涉及勾玉/付费资源）。

被拒绝时返回的 `approval_token` **在人工批准前不可用**：`AgentMemory.add_approval()` 只把请求写进 `pending`，启用它需要 `grant_approval(token)`——批准会把该请求从 `pending` **移**到 `tokens`（只保留一次），所以 token 消费掉之后不能被重新批准复活。批准动作**不出现在任何 MCP 工具或 REST 端点里**——否则弱 agent 可以拿着自己触发的 token 自我批准，闸门形同虚设。

### 7.3 让位（hold）

所有写工具第一道闸门。三种触发：聊天里说、`POST /api/agent/hold?minutes=120`、浏览器书签（一条 GET 链接）。hold 期间：不允许任何写操作、不允许启实例；到点自动解除。`emergency_stop` 会同时进 hold。

### 7.4 急停

`emergency_stop`：停所有实例 + 进 hold（默认 120 分钟）+ journal 记一笔。一个工具、一个 HTTP 端点、一个书签。

## 8. 错误处理

| 症状 | 处理 |
|---|---|
| 实例状态 WARNING | `get_log` 取末尾日志 → `file_issue` → 若 `on_failure` 允许则 `set_instance_running(true)` 重来一次 |
| 任务连续失败 ≥2 | 标记 skipped，`file_issue`，继续下一个 |
| 同实例 3 个任务连续失败 | `emergency_stop` + issue（要求人工） |
| 配置写入冲突（回读不一致） | 重试一次；仍失败则记 journal 并返回错误，不假装成功 |
| 子进程僵死（`pid_alive` 但状态不推进） | `set_instance_running(false)` → `true`（重启实例） |
| 帧缓存为空（刚启动 / 已关闭） | `screenshot` 返回明确错误，不返回旧图冒充新图 |
| MCP 与 OAS 断连 | DSH 侧自动重连（`mcp-client` 内置），工具短暂失败可重试 |

## 9. 测试策略

1. **纯函数单测**（pytest，无需设备）：`policy` 分级、`daily_plan` 解析与校验、`state` 合并迁移、`logtail` 游标与截断、`run_task_now` 的 next_run 计算。
2. **无设备集成测试**：`fastapi.testclient.TestClient` 直接打 `app`（不启 uvicorn、不启脚本子进程）；用 `config/template.json` 复制一个假实例，校验读写端点、回环中间件（伪造非本机 client host 应 403）、hold 闸门。
3. **MCP 冒烟**：用 `mcp` SDK 的客户端连 `http://127.0.0.1:<port>/mcp`，`list_tools` 数量与名称符合预期，调用 `list_instances` / `get_briefing` 成功。
4. **真机验收**（人工）：只跑 `WantedQuests` 一轮，验证插队、让位、日志游标、截图、「失败即跳过」。

## 10. 分期

| 期 | 内容 | 验收 |
|---|---|---|
| P1 | 能力层骨架 + `/api/agent/*` 只读 + `tests/agent` | TestClient 全绿；能 curl 出实例/队列/任务/日志 |
| P2 | MCP 薄壳 + `/mcp` 挂载 + 回环中间件 + hold | MCP 客户端能 list 并调用只读工具 |
| P3 | 写操作（调度干预、配置、启停）+ policy 分级 | 无设备集成测试全绿 |
| P4 | 命令队列 + 帧缓存 + 截图 + 设备命令 | 真机：停止后能截图、goto_main 生效 |
| P5 | daily_plan / state / journal / issue + playbook + headless 与常驻接线 | 一次完整当日循环 |

## 11. 风险与未决

| 风险 | 缓解 |
|---|---|
| FastAPI 挂载 Starlette 子应用时子应用 lifespan 不执行，MCP 会话管理器不跑 | `mcp_server.py` 模块级创建 `http_app = mcp.streamable_http_app()`；`app.py` 的 `lifespan` 里 `async with mcp.session_manager.run()`；`streamable_http_path` 设为 `/` 再 mount 到 `/mcp`，避免双前缀 |
| 配置并发写丢更新 | 读—改—立即写—回读校验 + 重试一次；风险写进工具描述 |
| 帧缓存线程读 `device.image` 与主线程替换引用 | 只读引用不调 `screenshot()`；引用替换是原子操作，最坏是拿到上一帧 |
| 命令队列在长任务期间不消费，agent 误以为已生效 | 工具返回 `effective: next_task_boundary`；长任务想立刻停就 `set_instance_running(false)` |
| `mcp` SDK 版本漂移（当前 1.27.1，`session_manager` 在调用 `streamable_http_app()` 后才可访问） | 在 `requirements-in.txt` 固定 `mcp` 版本 |
| `mcp` 1.x 的依赖与 OAS 原钉的 web 栈/Windows 依赖冲突：要求 `pydantic>=2.11`、`anyio>=4.5`、`pywin32>=310`，而原 `fastapi==0.104.1`（经 starlette 0.27）钉 `anyio<4`、`pydantic==2.10.0`、`pywin32==306`；`mcp 1.11` 虽然放宽了 pydantic，但会对 `-> Any` 的返回注解做 `issubclass` 而崩溃 | `requirements-in.txt` 同步提到 `fastapi==0.116.1`（其 starlette 允许 `anyio<5`）、`pydantic==2.11.0`、`pywin32==310`；`requirements.txt` 必须在 **Python 3.10** 环境用 `pip-compile` 重新生成（本机 3.13 无法编译钉死的 `numpy==1.24.3`）。未装 `mcp` 时 MCP 入口会记一条 warning 并跳过，REST 不受影响 |

## 12. 附：数据契约摘要

```jsonc
// GET /api/agent/instances
[{ "name":"oas1", "state":"RUNNING", "pid_alive":true,
   "running_task":"Orochi", "frame_age_s":2.1, "hold":{"active":false} }]

// GET /api/agent/{name}/snapshot
{ "name":"oas1", "state":"RUNNING", "running_task":"Orochi",
  "schedule":{"running":{"name":"Orochi","next_run":"..."},
              "pending":[{"name":"WantedQuests","next_run":"..."}],
              "waiting":[{"name":"SoulsTidy","next_run":"..."}]} }

// POST /api/agent/{name}/tasks/Orochi/run_now?position=front
{ "ok":true, "applied_at":"2026-10-23T18:15:00+08:00",
  "effective":"next_task_boundary", "next_run":"2026-10-23 18:15:00" }
```
