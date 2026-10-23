# This Python file uses the following encoding: utf-8
"""动作分级、hold 闸门、受保护任务判定。"""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Sequence

from module.config.utils import convert_to_underscore
from module.server.agent.models import HoldState

# 涉及勾玉/付费资源的任务，改动需要人工批准
PROTECTED_DEFAULT: tuple[str, ...] = ("mystery_shop", "rich_man", "talisman_pass")


class ActionClass(str, Enum):
    READ = "read"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


class PolicyDenied(Exception):
    """写操作被闸门拒绝。"""

    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


class HoldActive(PolicyDenied):
    """让位模式生效中，所有写操作被拒绝。"""


class NeedsApproval(PolicyDenied):
    """不可逆动作，需要人工批准。"""


_REVERSIBLE_KINDS = frozenset(
    {"instance", "command", "schedule_rule", "hold", "release_hold", "estop"}
)
_TASK_KINDS = frozenset({"task_enable", "task_args", "next_run", "run_now"})


def classify_write(
    kind: str,
    task: str | None = None,
    protected: Sequence[str] = PROTECTED_DEFAULT,
) -> ActionClass:
    """把一次写操作归为 reversible / irreversible；未知 kind 抛 ValueError。"""
    if kind in _REVERSIBLE_KINDS:
        return ActionClass.REVERSIBLE
    if kind in _TASK_KINDS:
        if task is None:
            return ActionClass.REVERSIBLE
        protected_set = {convert_to_underscore(item) for item in protected}
        if convert_to_underscore(task) in protected_set:
            return ActionClass.IRREVERSIBLE
        return ActionClass.REVERSIBLE
    raise ValueError(f"unknown action kind: {kind}")


class HoldGate:
    """内存态的让位开关，带过期时间。"""

    def __init__(self, now: Callable[[], datetime] = datetime.now) -> None:
        self._now = now
        self._until: datetime | None = None
        self._reason: str = ""

    def activate(self, minutes: int = 120, reason: str = "") -> HoldState:
        if minutes <= 0:
            raise ValueError("hold minutes must be positive")
        self._until = self._now() + timedelta(minutes=minutes)
        self._reason = reason
        return self.state()

    def release(self) -> HoldState:
        self._until = None
        self._reason = ""
        return self.state()

    def state(self) -> HoldState:
        if self._until is None:
            return HoldState(active=False)
        if self._now() >= self._until:
            self._until = None
            self._reason = ""
            return HoldState(active=False)
        return HoldState(active=True, until=self._until, reason=self._reason)
