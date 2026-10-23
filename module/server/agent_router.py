# This Python file uses the following encoding: utf-8
"""REST 入口 /api/agent/*，与 MCP 工具共用 AgentService。"""
from __future__ import annotations

import inspect
from functools import wraps
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from module.logger import logger
from module.server.agent.policy import HoldActive, NeedsApproval
from module.server.agent.service import AgentService, UnknownInstance


class RunningBody(BaseModel):
    running: bool


class CommandBody(BaseModel):
    command: str


class EnableBody(BaseModel):
    enabled: bool
    approval_token: str | None = None


class ArgsBody(BaseModel):
    group: str
    argument: str
    value: Any
    approval_token: str | None = None


class NextRunBody(BaseModel):
    at: str
    approval_token: str | None = None


class RunNowBody(BaseModel):
    position: str = "front"
    approval_token: str | None = None


class RuleBody(BaseModel):
    rule: str


class HoldBody(BaseModel):
    minutes: int = 120
    reason: str = ""


class StateBody(BaseModel):
    patch: dict


class JournalBody(BaseModel):
    text: str


class IssueBody(BaseModel):
    task: str
    symptom: str
    suggestion: str = ""
    instance: str | None = None


def _handle(fn):
    """所有端点统一转成 async：FastAPI 会把同步端点丢进线程池，返回 coroutine 会炸。"""

    @wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            return result
        except HTTPException:
            raise
        except Exception as error:  # noqa: BLE001
            raise _translate(error) from error

    return wrapper


def _translate(error: Exception) -> HTTPException:
    if isinstance(error, HTTPException):
        return error
    if isinstance(error, UnknownInstance):
        return HTTPException(status_code=404, detail=f"unknown instance: {error}")
    if isinstance(error, HoldActive):
        return HTTPException(status_code=423, detail={"error": "hold_active", **error.detail})
    if isinstance(error, NeedsApproval):
        return HTTPException(status_code=403, detail={"error": "needs_approval", **error.detail})
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return HTTPException(status_code=400, detail=str(error))
    logger.exception(f'agent API internal error: {error}')
    return HTTPException(status_code=500, detail='internal error')


def create_agent_router(service: AgentService) -> APIRouter:
    router = APIRouter(prefix="/api/agent", tags=["agent"])

    @router.get("/briefing")
    @_handle
    def briefing():
        return service.briefing()

    @router.get("/instances")
    @_handle
    def instances():
        return service.list_instances()

    @router.get("/{name}/snapshot")
    @_handle
    def snapshot(name: str):
        return service.snapshot(name)

    @router.get("/{name}/tasks")
    @_handle
    def tasks(name: str):
        return service.list_tasks(name)

    @router.get("/{name}/tasks/{task}/args")
    @_handle
    def task_args(name: str, task: str):
        return service.task_args(name, task)

    @router.get("/{name}/log")
    @_handle
    def log(name: str, cursor: int | None = None, limit: int = 200):
        return service.log(name, cursor=cursor, limit=limit)

    @router.get("/{name}/screenshot")
    @_handle
    def screenshot(name: str):
        payload = service.screenshot(name)
        return {"available": payload["available"], "age_s": payload["age_s"]}

    @router.get("/{name}/screenshot.jpg")
    @_handle
    def screenshot_jpg(name: str):
        from fastapi import Response

        payload = service.screenshot(name)
        if not payload["available"]:
            raise HTTPException(status_code=404, detail="no frame yet")
        return Response(content=payload["image"], media_type="image/jpeg")

    @router.post("/{name}/running")
    @_handle
    async def set_running(name: str, body: RunningBody):
        return await service.set_running(name, body.running)

    @router.post("/{name}/command")
    @_handle
    def command(name: str, body: CommandBody):
        return service.send_command(name, body.command)

    @router.post("/{name}/tasks/{task}/enable")
    @_handle
    def enable(name: str, task: str, body: EnableBody):
        return service.set_task_enabled(name, task, body.enabled, approval_token=body.approval_token)

    @router.post("/{name}/tasks/{task}/args")
    @_handle
    def args(name: str, task: str, body: ArgsBody):
        return service.set_task_arg(
            name, task, body.group, body.argument, body.value, approval_token=body.approval_token
        )

    @router.post("/{name}/tasks/{task}/next_run")
    @_handle
    def next_run(name: str, task: str, body: NextRunBody):
        return service.set_next_run(name, task, body.at, approval_token=body.approval_token)

    @router.post("/{name}/tasks/{task}/run_now")
    @_handle
    async def run_now(name: str, task: str, body: RunNowBody):
        return await service.run_now(
            name, task, position=body.position, approval_token=body.approval_token
        )

    @router.post("/{name}/schedule_rule")
    @_handle
    def schedule_rule(name: str, body: RuleBody):
        return service.set_schedule_rule(name, body.rule)

    @router.post("/hold")
    @_handle
    def hold(body: HoldBody):
        return service.hold(minutes=body.minutes, reason=body.reason)

    @router.delete("/hold")
    @_handle
    def release_hold():
        return service.release_hold()

    @router.post("/emergency_stop")
    @_handle
    async def emergency_stop(body: HoldBody | None = None):
        minutes = body.minutes if body else 120
        return await service.emergency_stop(minutes=minutes)

    @router.get("/issues")
    @_handle
    def list_issues(status: str | None = None):
        return service.list_issues(status=status)

    @router.post("/issues")
    @_handle
    def file_issue(body: IssueBody):
        return service.file_issue(
            task=body.task,
            symptom=body.symptom,
            suggestion=body.suggestion,
            instance=body.instance,
        )

    @router.post("/daily_state")
    @_handle
    def daily_state(body: StateBody):
        return service.update_daily_state(body.patch)

    @router.post("/journal")
    @_handle
    def journal(body: JournalBody):
        return service.journal(body.text)

    return router
