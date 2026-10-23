# OAS Agent 接入说明

## 1. 启动 OAS 后端

```bash
python server.py --port 22267
```

`/mcp` 与 `/api/agent/*` 只接受本机访问。

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

```bash
mkdir -p data/agent
cp config/agent/daily_plan.example.yaml data/agent/daily_plan.yaml
```

编辑 `must` / `optional` / `protected_tasks` / `wake`。

## 4. 两种拉起方式

- **常驻**：在一个专用会话里让 agent 建一个每 15 分钟的提醒做巡检。
- **headless**：Windows 任务计划执行
  `dsh --profile headless "按 docs/agent/daily-playbook.md 巡检一轮，先调 mcp__oas__get_briefing"`。

工作目录固定为 `data/agent/workspace/`：它写不到 `module/`，改不了源码。

## 5. 人在玩的时候

- 聊天里说「让位」，agent 调 `hold_device`；
- 或者在本机发一条 POST（只接受 JSON body，查询参数 `?minutes=` 不生效）：

  ```powershell
  Invoke-RestMethod -Method Post -Uri http://127.0.0.1:22267/api/agent/hold `
    -ContentType 'application/json' -Body '{"minutes":120,"reason":"human playing"}'
  ```

  `minutes` 默认 120，到点自动解除。
- 提前结束让位：`Invoke-RestMethod -Method Delete -Uri http://127.0.0.1:22267/api/agent/hold`
- 急停：`Invoke-RestMethod -Method Post -Uri http://127.0.0.1:22267/api/agent/emergency_stop`（停掉全部实例并进入让位）。

## 6. 出问题看哪里

- `data/agent/journal/<date>.md`：agent 的决策理由；
- `data/agent/issues/ISS-*.md`：阻塞性问题，含快照、日志片段、截图；
- `data/agent/state.json`：今天做到哪了。
