# This Python file uses the following encoding: utf-8
"""MCP 入口：把 AgentService 包成工具。"""
from __future__ import annotations

import asyncio
import json
from functools import wraps
from typing import Any

from mcp.server.fastmcp import FastMCP

from module.server.agent.policy import PolicyDenied
from module.server.agent.service import AgentService

INSTRUCTIONS = (
    "OAS 游戏自动化控制台。所有工具都是非阻塞的：写操作不会打断正在运行的任务，"
    "改动在下一个任务边界生效；要立刻停下某个实例，用 set_instance_running(false)。"
    "每日编排前先调用 get_briefing 拿到计划、当日状态和实例快照。"
    "任务失败两次就跳过并 file_issue，不要反复重试同一个任务。"
    "「hold is active」表示人正在使用设备：所有写操作都会被拒绝，你只需结束本轮并等它过期；解除让位只能由人通过本机接口完成。"
    "「human approval required」表示该任务受保护，需要人在会话里批准后带上返回的 approval_token 重试。"
)

TOOL_NAMES = (
    "get_briefing",
    "list_instances",
    "get_snapshot",
    "list_tasks",
    "get_task_args",
    "get_log",
    "screenshot",
    "set_instance_running",
    "send_device_command",
    "set_task_enabled",
    "set_task_args",
    "set_task_next_run",
    "run_task_now",
    "set_schedule_rule",
    "hold_device",
    "emergency_stop",
    "update_daily_state",
    "log_decision",
    "file_issue",
    "list_issues",
)


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _gate_errors(fn):
    """把闸门拒绝转成结构化 JSON，让模型能看到 approval_token / hold 原因。"""

    def _render(error) -> str:
        return _dump(
            {
                "ok": False,
                "error": type(error).__name__,
                "message": str(error),
                "detail": getattr(error, "detail", {}) or {},
            }
        )

    if asyncio.iscoroutinefunction(fn):
        @wraps(fn)
        async def async_wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except PolicyDenied as error:
                return _render(error)

        return async_wrapper

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except PolicyDenied as error:
            return _render(error)

    return wrapper


def create_mcp(service: AgentService) -> FastMCP:
    mcp = FastMCP(
        name="oas",
        instructions=INSTRUCTIONS,
        streamable_http_path="/",
        stateless_http=True,
    )

    @mcp.tool()
    def get_briefing() -> str:
        """一次性拿到：实例列表、hold 状态、daily_plan、今日 state、playbook 摘要。"""
        return _dump(service.briefing())

    @mcp.tool()
    def list_instances() -> str:
        """列出所有 OAS 实例的运行状态。"""
        return _dump(service.list_instances())

    @mcp.tool()
    def get_snapshot(instance: str) -> str:
        """读取某实例的状态与调度队列（running/pending/waiting）。"""
        return _dump(service.snapshot(instance))

    @mcp.tool()
    def list_tasks(instance: str) -> str:
        """列出某实例中所有可调度任务的开关、next_run、优先级。"""
        return _dump(service.list_tasks(instance))

    @mcp.tool()
    def get_task_args(instance: str, task: str) -> str:
        """读取某个任务的参数 schema 与当前值。"""
        return _dump(service.task_args(instance, task))

    @mcp.tool()
    def get_log(instance: str, cursor: int | None = None, limit: int = 200) -> str:
        """增量读取日志。第一次不传 cursor 拿末尾若干行；之后传回返回的 cursor。"""
        return _dump(service.log(instance, cursor=cursor, limit=limit))

    @mcp.tool()
    def screenshot(instance: str) -> Any:
        """返回该实例最新一帧游戏画面（可能落后几秒）。没有帧时返回说明文字。"""
        from mcp.server.fastmcp import Image

        payload = service.screenshot(instance)
        if not payload["available"]:
            return "no frame yet: 实例刚启动或帧发布已关闭"
        return Image(data=payload["image"], format="jpeg")

    @mcp.tool()
    @_gate_errors
    async def set_instance_running(instance: str, running: bool) -> str:
        """启动或停止实例子进程。停止是唯一能立刻中断当前任务的操作。"""
        return _dump(await service.set_running(instance, running))

    @mcp.tool()
    @_gate_errors
    def send_device_command(instance: str, command: str) -> str:
        """下发设备命令，下一个任务边界生效。command: goto_main/restart_game/stop_game/emulator_start/emulator_stop。"""
        return _dump(service.send_command(instance, command))

    @mcp.tool()
    @_gate_errors
    def set_task_enabled(instance: str, task: str, enabled: bool, approval_token: str | None = None) -> str:
        """开关某个任务。受保护任务（勾玉/付费相关）需要人工批准后带 approval_token 调用。"""
        return _dump(service.set_task_enabled(instance, task, enabled, approval_token=approval_token))

    @mcp.tool()
    @_gate_errors
    def set_task_args(
        instance: str,
        task: str,
        group: str,
        argument: str,
        value: Any,
        approval_token: str | None = None,
    ) -> str:
        """修改任务参数，例如 group=scheduler argument=enable。"""
        return _dump(
            service.set_task_arg(
                instance, task, group, argument, value, approval_token=approval_token
            )
        )

    @mcp.tool()
    @_gate_errors
    def set_task_next_run(instance: str, task: str, at: str, approval_token: str | None = None) -> str:
        """设置下次运行时间，格式 'YYYY-MM-DD HH:MM:SS'。"""
        return _dump(service.set_next_run(instance, task, at, approval_token=approval_token))

    @mcp.tool()
    @_gate_errors
    async def run_task_now(
        instance: str, task: str, position: str = "front", approval_token: str | None = None
    ) -> str:
        """立刻补跑某任务。position=front 插到队首，end 排到 5 分钟后。必要时自动启动实例。"""
        return _dump(
            await service.run_now(instance, task, position=position, approval_token=approval_token)
        )

    @mcp.tool()
    @_gate_errors
    def set_schedule_rule(instance: str, rule: str) -> str:
        """设置调度规则：Filter / FIFO / Priority。"""
        return _dump(service.set_schedule_rule(instance, rule))

    @mcp.tool()
    def hold_device(minutes: int = 120, reason: str = "") -> str:
        """让位：人要用设备时调用，期间所有写操作被拒绝，到点自动解除。"""
        return _dump(service.hold(minutes=minutes, reason=reason))

    @mcp.tool()
    @_gate_errors
    async def emergency_stop(minutes: int = 120) -> str:
        """急停：停止所有实例并进入让位。"""
        return _dump(await service.emergency_stop(minutes=minutes))

    @mcp.tool()
    def update_daily_state(patch: dict) -> str:
        """合并写入 data/agent/state.json，记录今天做了什么、跳过了什么。"""
        return _dump(service.update_daily_state(patch))

    @mcp.tool()
    def log_decision(text: str) -> str:
        """在当日编排日记里追加一行决策理由。"""
        return _dump(service.journal(text))

    @mcp.tool()
    def file_issue(task: str, symptom: str, suggestion: str = "", instance: str | None = None) -> str:
        """记录一个阻塞性问题（含快照、日志片段、截图），交给人工用更强的模型复现。"""
        return _dump(
            service.file_issue(
                task=task, symptom=symptom, suggestion=suggestion, instance=instance
            )
        )

    @mcp.tool()
    def list_issues(status: str | None = None) -> str:
        """列出已记录的 issue，status 可选 open/resolved/wontfix。"""
        return _dump(service.list_issues(status=status))

    return mcp
