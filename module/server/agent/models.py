# This Python file uses the following encoding: utf-8
"""agent 能力层的数据契约。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

EFFECTIVE_IMMEDIATE = "immediate"
EFFECTIVE_NEXT_BOUNDARY = "next_task_boundary"


@dataclass
class HoldState:
    active: bool
    until: datetime | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "until": self.until.strftime("%Y-%m-%d %H:%M:%S") if self.until else None,
            "reason": self.reason,
        }


@dataclass
class InstanceView:
    name: str
    state: str
    pid_alive: bool
    running_task: str = ""
    frame_age_s: float | None = None
    hold_active: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Snapshot:
    name: str
    state: str
    running_task: str = ""
    schedule: dict[str, Any] = field(default_factory=dict)
    hold: HoldState = field(default_factory=lambda: HoldState(active=False))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "running_task": self.running_task,
            "schedule": self.schedule,
            "hold": self.hold.to_dict(),
        }


@dataclass
class WriteResult:
    ok: bool
    applied_at: datetime
    effective: str = EFFECTIVE_NEXT_BOUNDARY
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "applied_at": self.applied_at.strftime("%Y-%m-%dT%H:%M:%S"),
            "effective": self.effective,
            "detail": self.detail,
        }
