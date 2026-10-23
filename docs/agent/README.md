# OAS Agent 接入说明

## 1. 启动 OAS 后端

> **依赖**：`mcp` 入口需要 `requirements-in.txt` 里新的 web 栈（fastapi 0.116.1 / pydantic 2.11 / pywin32 310）。
> `requirements.txt` 是 pip-compile 生成的锁文件，必须在 **Python 3.10** 环境重新生成并提交：
> ```powershell
> pip-compile --annotation-style=line --output-file=requirements.txt requirements-in.txt
> ```
> 不重新生成就安装，`/mcp` 会因为缺 `mcp` 而静默禁用（REST 仍可用）。

```bash
python server.py --port 22267
```

`/mcp` 与 `/api/agent/*` 只接受本机访问。起服务后可用 `python scripts/agent_smoke.py` 做只读冒烟。

## 2. 把 MCP 挂到 DSH

写入 `$DSH_HOME/cordis.patch.yml`（Windows 默认 `%USERPROFILE%\.dsh\cordis.patch.yml`），或启动时 `--patch` 指向一个文件：

```yaml
- insert:
    - id: mcp-oas
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: oas
        transport: streamable-http
        url: http://127.0.0.1:22267/mcp
        toolCallTimeoutMs: 30000
```

改完必须重启 DSH，工具才会以 `mcp__oas__*` 出现。

## 3. 每日计划

```powershell
New-Item -ItemType Directory -Force -Path data/agent | Out-Null
Copy-Item config/agent/daily_plan.example.yaml data/agent/daily_plan.yaml
```

编辑 `must` / `optional` / `protected_tasks` / `wake`；`budget.quiet_hours` 和 `on_failure.retry` 也会被 playbook 读取。

## 4. 两种拉起方式

- **常驻**：在一个专用会话里让 agent 建一个每 15 分钟的提醒做巡检。
- **headless**：Windows 任务计划，每天按 `daily_plan.yaml` 的 `wake` 时刻各跑一次，起始目录见 4.1：
  `dsh --profile headless "先调用 mcp__oas__get_briefing，然后按返回的 playbook 字段执行本轮巡检"`

## 4.1 沙箱：先钉住工作目录（必做）

「agent 改不了源码」不是靠提示词，而是靠**把 agent 的工作目录钉在 `data/agent/workspace/`**。DSH 只允许写当前工作目录之内；`AgentMemory` 只负责创建这个目录，**不会**改变任何进程的 cwd——必须由你在启动时指定。

- **headless（任务计划）**：动作里必须同时设置程序与起始目录。
  - 程序：`dsh`
  - 参数：`--profile headless "先调用 mcp__oas__get_briefing，然后按返回的 playbook 字段执行本轮巡检"`
  - **起始于（Start in）**：`D:\dev\OnmyojiAutoScript\data\agent\workspace`
- **常驻会话**：先在 PowerShell 里 `cd D:\dev\OnmyojiAutoScript\data\agent\workspace`，再启动 DSH。

验证（做一次就够）：在该目录下启动的会话里让 agent 尝试写 `../../module/__probe.txt`；它必须被拒绝。若写入成功，说明工作目录没钉住，立即停止无人值守运行。

注意：工作目录不在仓库根，所以**不要让 agent 去读仓库里的 playbook 文件**——`get_briefing` 的 `playbook` 字段已经带上同样内容。

## 5. 人在玩的时候

- 聊天里说「让位」，agent 调 `hold_device`；
- **agent 不能解除让位**：`release_device` 工具已移除，解除只能由人通过本机接口 `DELETE /api/agent/hold` 完成；
- 注意：让位**不会**停下已经在跑的任务（它只拒绝写操作）；要立刻腾出设备，用 `emergency_stop`（或直接停掉对应实例）；
- 或者在本机发一条 POST（只接受 JSON body，查询参数 `?minutes=` 不生效）：

  ```powershell
  Invoke-RestMethod -Method Post -Uri http://127.0.0.1:22267/api/agent/hold `
    -ContentType 'application/json' -Body '{"minutes":120,"reason":"human playing"}'
  ```

  `minutes` 默认 120，到点自动解除。
- 提前结束让位：`Invoke-RestMethod -Method Delete -Uri http://127.0.0.1:22267/api/agent/hold`
- 急停：`Invoke-RestMethod -Method Post -Uri http://127.0.0.1:22267/api/agent/emergency_stop`（停掉全部实例并进入让位）。

## 5.1 批准受保护任务（一次性）

`protected_tasks` 里的任务（默认 `MysteryShop` / `RichMan` / `TalismanPass`）被写入时，接口返回 403，`detail.approval_token` 里是待批令牌。**这个令牌在被批准前不可用**——agent 拿到它也不能自己用，这是防止弱模型自我放行的关键。

批准没有工具入口，只能在本机手动执行：

```powershell
python -c "from module.server.agent.memory import AgentMemory; print(AgentMemory().grant_approval('<token>'))"
```

批准后把 token 交给 agent（或自己在请求里带 `approval_token`），它只能用一次。状态在 `data/agent/approvals.json`：`pending` 待批，`tokens` 已批未用。

## 6. 出问题看哪里

- `data/agent/journal/<date>.md`：agent 的决策理由；
- `data/agent/issues/ISS-*.md`：阻塞性问题，含快照、日志片段、截图（`file_issue` 只在当时有画面帧时才附带 `.jpg`）；
- `data/agent/state.json`：今天做到哪了。
