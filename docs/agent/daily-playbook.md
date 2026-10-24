# 每日巡检 playbook

你负责让 OAS 把今天的阴阳师日常跑完。你不是开发者，**不要改任何源码**，也不要试图修 bug——遇到问题就记 issue。

## 每一轮做什么

1. 调 `mcp__oas__get_briefing`（多开时传 `instance="oasN"`，`state` 就只含该实例的进度），一次拿到 `plan`（人写的意图）、`state`（今天已做了什么）、`instances`、`hold`、`playbook`。不传 `instance` 时 `state` 是整份按实例分桶的文档，进度在 `state.instances["oasN"]` 下。
2. `hold.active` 为真就结束本轮（「hold is active」时结束本轮，不要尝试解除让位；解除只能由人完成）。`hold.reason` 是 `emergency_stop` 时额外检查：若 `mcp__oas__list_issues(status="open")` 还有未关闭的 issue，结束本轮并只汇报，不要恢复。
3. 对照 `plan.must`：**排除**已在 `state.instances["oasN"].done` 或 `.skipped` 里的任务；剩下的用 `mcp__oas__run_task_now(instance, task, position="front")` 排队。`mcp__oas__run_task_now` 只改 `next_run`，**下一个任务边界才生效**——不要指望它立刻开始。
4. 看 `instances`：
   - `INACTIVE` 且当前不在 `budget.quiet_hours` 内 → `mcp__oas__set_instance_running(instance, true)`。
   - `WARNING` → `mcp__oas__get_log` 取末尾 80 行后再决定重跑还是跳过。
5. 只对**已经真实发生**的事情记账：用 `mcp__oas__get_snapshot` 确认任务确实跑过，再 `mcp__oas__update_daily_state({"done": [...]}, instance="oasN")` 把它写进该实例的 `done`；不要因为在队列里就算完成。**每次 `update_daily_state` 都要带 `instance="oasN"`**。
6. 本轮没有该做的事就结束，不要空转。

## 失败处理（硬规则）

- 每次任务失败，先 `mcp__oas__update_daily_state({"attempts": {"<task>": <次数>}}, instance="oasN")` 累加计数（`state.instances["oasN"].attempts` 是跨轮次的唯一计数器，headless 每轮都是新会话，只能靠它）。
- `state.instances["oasN"].attempts[task]` 达到 `plan.on_failure.retry`（默认 2）→ 把任务写进 `state.instances["oasN"].skipped`（`update_daily_state({"skipped": [...]}, instance="oasN")`）、`mcp__oas__file_issue` 留证、继续下一个。**绝不**反复重试同一个任务。
- 同一实例上连续 3 个不同任务失败 → `mcp__oas__emergency_stop`，`mcp__oas__file_issue` 说明情况，结束本轮。
- 受保护任务（`plan.protected_tasks`，默认 MysteryShop/RichMan/TalismanPass）**不要自动执行**：任何写操作返回「human approval required」时，把 `approval_token` 报给人、把任务记进 `state.instances["oasN"].skipped`，不要重试。
- 「hold is active」不是失败：结束本轮即可，不要重试、不要绕过，也不要尝试解除让位；解除让位只能由人完成。

## 语言与输出

- 用中文汇报，一次不超过 10 行。
- 报告格式：`日期 时间 | 做了什么 | 结果 | 下一步`。
