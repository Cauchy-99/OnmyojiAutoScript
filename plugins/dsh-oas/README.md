# dsh-oas

DSH 插件：把 OAS 的每日 / 每周编排变成**确定性代码**，只在异常时把控制权交还给模型。

## 它做什么 / 不做什么

| 做 | 不做 |
|---|---|
| 每 `tickSeconds` 读一次 `/api/agent/briefing` 与任务列表 | 不读 OAS 配置文件、不 import OAS 代码 |
| 按 `daily_plan.yaml` 决定「启实例 / 开任务 / 排任务」并下发 | 不重复实现调度器——真正跑任务的是 OAS |
| 失败、受保护任务、连续失败 → 写 escalation | 不自己解除让位、不自己批准受保护任务 |
| 暴露三个工具给模型：`oas_status` / `oas_escalations` / `oas_escalation_resolve` | 不做任何 hold/审批旁路（全部走 OAS 的闸门） |

## 安装

```powershell
dsh plugin --profile web add file:D:/dev/OnmyojiAutoScript/plugins/dsh-oas
```

插件包自带 `cordis.patch.yml`（`dsh.bundle.patch`），安装后会自动插入一行。也可以手工把它 `insert` 到 `$DSH_HOME/profiles/web/cordis.patch.yml`：

```yaml
- insert:
    - id: dsh-oas
      name: dsh-plugin-oas
      config:
        baseUrl: http://127.0.0.1:22267
        instances: [oas1]
        tickSeconds: 60
        enable: true
```

改完重启 DSH。工具以 `oas_status`、`oas_escalations`、`oas_escalation_resolve` 出现在模型侧。

## 配置

| 字段 | 默认 | 说明 |
|---|---|---|
| `baseUrl` | `http://127.0.0.1:22267` | OAS 后端地址；只支持回环（OAS 侧守卫会拒绝非回环 Host） |
| `instances` | `["oas1"]` | 要编排的实例名，必须与 `config/oasN.json` 对应 |
| `tickSeconds` | `60` | 轮询间隔，最小 30 |
| `stateDir` | `~/.dsh/oas-escalations` | escalation 落盘目录，绝对路径，避免依赖插件的工作目录 |
| `enable` | `true` | `false` 只注册工具、不跑定时器（用于手工验证） |

## 决策规则（`src/plan.ts`，纯函数）

按顺序命中即返回：

1. `hold.active` → 零动作（让位期间不打扰人）；
2. 实例不存在 → escalation `state_unreadable`；
3. 实例 `WARNING` → escalation `instance_warning`；
4. 实例 `INACTIVE` 且在 `quiet_hours` 外 → 启实例；
5. 某任务 `attempts >= on_failure.retry` 且未 skipped → escalation `retry_exhausted`；
6. `protected_tasks` 里的任务到期 → escalation `protected_task`（永不自动执行）；
7. weekly 条目命中今天且本周未完成 → 排到队首；
8. `must` 里第一个未完成、未跳过、未达重试上限的任务：禁用则先开，否则排到队首；
9. 已有 3 个任务出现过失败 → `emergency_stop`；
10. 否则零动作。

## 每周任务

`data/agent/daily_plan.yaml`：

```yaml
weekly:
  - { task: TrueOrochi, days: [1, 5] }   # 1=周一 … 7=周日
  - { task: Secret, days: [3] }
  - { task: Duel, days: [6, 7] }
```

完成状态写在 `data/agent/state.json` 的 `weekly` 桶（`{"oas1": {"TrueOrochi": "2026-W43"}}`），跨天不会被重置。插件只负责「本周还没做就排一次」，真正「做完」由模型在确认任务成功后写回：

```
oas_status → 看到任务已完成 → POST /api/agent/daily_state {"patch": {"weekly": {...}}}
```

## 异常升级（agent 介入）

插件不自己修问题，只把证据落盘：

```
~/.dsh/oas-escalations/ESC-20261024-101500-retry_exhausted.json
```

模型侧：

1. `oas_escalations(status="open")` 拿到列表；
2. 判断：重启任务（`set_task_enabled` + `run_task_now`）、跳过（写 `state.skipped`）、或叫人（REST 通知）；
3. 处理后 `oas_escalation_resolve(id, note)` 关掉。

同一 `code + instance + task` 只保留一条未处理的 escalation，避免刷屏。

## 与 OAS 的契约

只用 `/api/agent/*`。因此以下行为都继承自 OAS，插件无法绕过：

- 写操作在**下一个任务边界**生效（`effective: next_task_boundary`）；
- hold 期间普通写 423、停实例放行；
- 受保护任务 403 + `approval_token`，token 需人工 `grant_approval` 后才可用；
- 非回环 `Host`/`Origin` 一律 403。

## 开发

```powershell
pnpm --dir plugins/dsh-oas install
pnpm --dir plugins/dsh-oas build
pnpm --dir plugins/dsh-oas test
```

零运行时依赖：工具用原始 `ToolDefinition`（`parameters` + `output.schema/render` + `execute`）注册，配置在插件内校验，因此不绑定 DSH 的版本。

### 迭代安装用 `link:`，不要用 `file:`

`file:` 会让 pnpm 把包**拷贝**进 profile，之后重建 `lib/` 不会同步，必须改版本号或删了重装。用软链则改完 `pnpm build` 即生效：

```powershell
pnpm dsh plugin --profile web remove dsh-plugin-oas
pnpm dsh plugin --profile web add link:D:/dev/OnmyojiAutoScript/plugins/dsh-oas
```

### 三个已经踩过的坑

1. **必须声明 `export const inject = ['tools']`**。Cordis 在取 `ctx.tools` 前会检查依赖，缺了会在加载时报 `cannot get property "tools" without inject`，插件静默不工作。
2. **`package.json` 必须是 UTF-8 无 BOM**。带 BOM 会被 DSH 的安装校验拒绝（`Unexpected token '﻿'`）。PowerShell 的 `Set-Content -Encoding utf8` 会写入 BOM，改用 `[System.IO.File]::WriteAllText($p, $text, (New-Object System.Text.UTF8Encoding $false))`。
3. **`config` 改动会热重载，代码改动需要重启 host**。验证阶段先把 `enable: false`，只调三个工具；确认后再打开。

### 手工验证一次

```powershell
python server.py -p 22267          # 真实后端或 stub 均可
```

然后在 DSH 会话里：`oas_status(instance="oas1")` → briefing JSON；`oas_escalations()` → `[]`；`oas_escalation_resolve(id="nope")` → `{"ok": false}`。

