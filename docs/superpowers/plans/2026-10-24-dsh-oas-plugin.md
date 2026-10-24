# dsh-oas 编排插件实施计划

**目标**：一个 DSH 插件（TypeScript，放本仓库 `plugins/dsh-oas/`），确定性地执行每日/每周编排，只在异常时把控制权交还模型。

**边界**：插件只通过 OAS 的 REST（`/api/agent/*`）工作，不读配置文件、不 import OAS 代码。因此 hold、审批、回环守卫全部照旧生效，插件没有任何旁路。

**已确认的 DSH 契约**：
- 插件包 = npm 包，`main` 导出 `name` 与 `apply(ctx)`；
- 要能被 `dsh plugin add` 自动组合，需要在 `package.json` 声明 `"dsh": { "bundle": { "patch": "./cordis.patch.yml" } }`，patch 里 `insert` 自己；
- 工具通过 `ctx.tools.register()` 注册；定时器用 `ctx.effect()` 返回 dispose。

**任务**

| # | 内容 | 验收 |
|---|---|---|
| 1 | OAS 侧：`state.json` 的 `weekly` 桶必须跨天保留 | `memory._load_state` 跨天重置时不丢 `weekly`；新增测试 |
| 2 | 插件骨架：`package.json` / `tsconfig.json` / `cordis.patch.yml` / `src/index.ts`，注册一个 `oas_status` 只读工具 | `pnpm build` 通过；`node -e` 能 import 出 `name/apply`；patch 里 insert 自己 |
| 3 | `src/client.ts`：`/api/agent/*` 的类型化客户端（`briefing`、`listInstances`、`runTaskNow`、`setNextRun`、`setTaskEnabled`、`setRunning`、`emergencyStop`、`updateState`、`listEscalations`） | 用假 fetch 的 vitest 覆盖：URL/方法/body、423/403/404 的语义化错误 |
| 4 | `src/plan.ts`：纯函数 `decide(briefing, now) -> Action[]`，覆盖 daily `must` 与 weekly `days` | 单测：hold 时零动作；INACTIVE 先 start；must 未完成才排；skipped 不重排；weekly 只在指定星期且本周未完成时排；受保护任务永不自动排 |
| 5 | `src/tick.ts` + `src/supervisor.ts`：一轮执行与常驻定时器；异常写 `data/agent/escalations/<id>.json` | 单测：假客户端断言调用序列；异常分类（WARNING / 连续失败 / 受保护 / 到达重试上限） |
| 6 | `README.md` + 安装脚本：`dsh plugin --profile web add file:<repo>/plugins/dsh-oas`，配置示例 | README 命令可直接复制执行；`oas_status` 能在 DSH 会话里被调用 |

**关键规则（写进 decide，不交给模型）**

- `hold.active` → 零动作；
- 实例 `INACTIVE` 且不在 `quiet_hours` → `start_instance`；
- `must` 中未在 `done`/`skipped` → `run_task(front)`，每 tick 每实例最多一个；
- `attempts[task] >= on_failure.retry` → 不再重试，写 escalation；
- 同实例连续 3 个任务失败 → `emergency_stop` + escalation；
- `protected_tasks` → 永不自动执行，写 escalation 等人工批准；
- weekly 条目：`days` 含今天且 `state.weekly[task] != 本周 ISO 周` → 排一次；完成后写回 `weekly`。

**不改动的部分**：OAS 的调度器、闸门、MCP 工具面；插件的 escalation 只是文件，模型通过 `list_escalations`（Task 5 附带注册）读取。

**测试与构建**：`vitest`（devDependency）；`pnpm --dir plugins/dsh-oas test`。
