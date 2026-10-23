# OAS Agent 入口验收清单

在 **Python 3.10** 的 OAS 环境上逐项执行。每项都给出命令与期望输出；任何一项不符就先停下，不要继续后面的步骤。

## 0. 前置：锁文件必须在 3.10 重新生成

```powershell
pip-compile --annotation-style=line --output-file=requirements.txt requirements-in.txt
git diff --stat requirements.txt
```

- [ ] `requirements.txt` 里出现 `mcp==1.27.1`、`fastapi==0.116.1`、`pydantic==2.11.x`、`pywin32==310`、`anyio>=4.5`
- [ ] `pip install -r requirements.txt` 无解析冲突

若这步没做，`/mcp` 会静默禁用（REST 仍可用），后面第 2、3 项必然失败。

## 1. 服务能起、REST 只读可用

```powershell
python server.py --port 22267
```

另开一个终端：

```powershell
python scripts/agent_smoke.py
```

- [ ] 5 行全部 `PASS`
- [ ] 退出码为 0（`$LASTEXITCODE`）

## 2. MCP 端点在

```powershell
# 用本机回环地址，Host 也必须是回环
Invoke-WebRequest -Method Post -Uri http://127.0.0.1:22267/mcp `
  -Headers @{ Host = '127.0.0.1:22267'; Accept = 'application/json, text/event-stream' } `
  -ContentType 'application/json' `
  -Body '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2026-07-28","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'
```

- [ ] 返回 200（或先 307 到 `/mcp/` 再 200），响应体里含 `serverInfo`
- [ ] 用非回环 Host 重试（`Host: rebind.example`）返回 **403**

## 3. DSH 能看到 20 个工具

在 MCP patch 生效并重启 DSH 后，新开一个会话，让它调 `mcp__oas__list_instances`。

- [ ] 工具列表里有 `mcp__oas__*` 共 **20** 个
- [ ] 没有 `mcp__oas__release_device`
- [ ] `mcp__oas__get_briefing` 返回里有 `plan`、`state`、`hold`、`playbook` 四个键

## 4. 一次真实的补跑

先确认实例在跑，然后：

```text
mcp__oas__run_task_now(instance="oas1", task="WantedQuests", position="front")
```

- [ ] 返回 `ok: true`，`effective: "next_task_boundary"`
- [ ] `mcp__oas__get_snapshot("oas1")` 里 `pending` 的第一项是 `wanted_quests`
- [ ] 任务边界到达后，`running_task` 变成 `wanted_quests`

## 5. 让位与急停

- [ ] `mcp__oas__hold_device(minutes=5)` 之后，`mcp__oas__set_task_enabled(...)` 返回 `{"ok": false, "error": "HoldActive"}`
- [ ] hold 期间 `mcp__oas__set_instance_running("oas1", false)` **仍然成功**（停实例是允许的）
- [ ] `DELETE /api/agent/hold` 能解除；agent 侧没有解除工具
- [ ] `POST /api/agent/emergency_stop` 停掉全部实例，且 `get_briefing` 显示 `hold.active = true`

## 6. 受保护任务

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:22267/api/agent/oas1/tasks/MysteryShop/enable `
  -Headers @{ Host = '127.0.0.1:22267' } -ContentType 'application/json' -Body '{"enabled":true}'
```

- [ ] 返回 403，`detail.approval_token` 非空
- [ ] 带同一个 token 重试仍然 403（未批准不可用）
- [ ] `python -c "from module.server.agent.memory import AgentMemory; print(AgentMemory().grant_approval('<token>'))"` 返回 `True` 后，带 token 重试 → 200
- [ ] 换一个受保护任务（`RichMan`）再拿旧 token 重试 → 403（token 绑定动作）

## 7. 证据链

- [ ] `data/agent/journal/<今日>.md` 有决策行
- [ ] 手动让一个任务失败两次后，`data/agent/state.json` 的 `skipped` 含它、`issues/` 下有 `ISS-*.md`，且 `.md` 里有快照与日志片段
- [ ] 跨天（或改系统日期）后重开，`state.json` 的 `date` 更新、`done`/`attempts` 归零，journal 里有一行「跨天」

## 失败时的排查顺序

1. `/mcp` 不在 → 先看第 0 项锁文件；再看启动日志有没有 `agent MCP endpoint disabled`
2. 403 一片 → 检查 `Host` 头是否为回环、请求是否来自本机
3. 写操作 423 → hold 还开着，先 `DELETE /api/agent/hold`
4. 403 + `needs_approval` → 这是设计行为，按第 6 项走人工批准
5. 截图一直 `available: false` → 实例是否在跑；帧发布线程只在脚本子进程里启动
