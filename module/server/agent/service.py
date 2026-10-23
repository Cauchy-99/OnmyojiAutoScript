# This Python file uses the following encoding: utf-8
"""agent 能力层：REST 与 MCP 共用的业务函数。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from module.server.agent.bridge import Commander, FrameStore
from module.server.agent.logtail import LogTailer
from module.server.agent.memory import AgentMemory
from module.server.agent.models import (
    EFFECTIVE_IMMEDIATE,
    EFFECTIVE_NEXT_BOUNDARY,
    InstanceView,
    Snapshot,
    WriteResult,
)
from module.server.agent.policy import HoldGate


class UnknownInstance(KeyError):
    pass


def _default_config_factory(manager: Any) -> Callable[[str], Any]:
    return manager.config_cache


class AgentService:
    def __init__(
        self,
        manager: Any,
        *,
        memory: AgentMemory | None = None,
        tailer: LogTailer | None = None,
        frame_store: FrameStore | None = None,
        commander: Commander | None = None,
        config_factory: Callable[[str], Any] | None = None,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.manager = manager
        self.memory = memory if memory is not None else AgentMemory()
        self.tailer = tailer if tailer is not None else LogTailer()
        self.frame_store = frame_store if frame_store is not None else FrameStore()
        self.commander = commander if commander is not None else Commander(manager)
        self._config_factory = config_factory or _default_config_factory(manager)
        self._now = now
        self._hold = HoldGate(now)

    # ------------------------------------------------------------ plumbing
    def _process(self, name: str) -> Any:
        processes = getattr(self.manager, "script_process", {}) or {}
        if name not in processes:
            raise UnknownInstance(name)
        return processes[name]

    def _config(self, name: str) -> Any:
        self._process(name)
        return self._config_factory(name)

    @staticmethod
    def _state_name(process: Any) -> str:
        state = getattr(process, "state", None)
        return str(getattr(state, "name", state))

    def _hold_dict(self) -> dict:
        return self._hold.state().to_dict()

    # ---------------------------------------------------------------- read
    def list_instances(self) -> list[dict]:
        views = []
        processes = getattr(self.manager, "script_process", {}) or {}
        for name, process in processes.items():
            running_task = ""
            try:
                running_task = self._config(name).model.dict().get("running_task", "") or ""
            except Exception:
                running_task = ""
            age = self.frame_store.age()
            views.append(
                InstanceView(
                    name=name,
                    state=self._state_name(process),
                    pid_alive=bool(process.is_alive()),
                    running_task=running_task,
                    frame_age_s=round(age, 1) if age is not None else None,
                    hold_active=self._hold.state().active,
                ).to_dict()
            )
        return views

    def snapshot(self, name: str) -> dict:
        process = self._process(name)
        config = self._config(name)
        try:
            config.update_scheduler()
            schedule = config.get_schedule_data()
        except Exception as error:
            schedule = {"error": str(error), "running": {}, "pending": [], "waiting": []}
        snapshot = Snapshot(
            name=name,
            state=self._state_name(process),
            running_task=config.model.dict().get("running_task", "") or "",
            schedule=schedule,
            hold=self._hold.state(),
        )
        return snapshot.to_dict()

    def list_tasks(self, name: str) -> list[dict]:
        data = self._config(name).model.dict()
        results = []
        for key, value in data.items():
            if not isinstance(value, dict) or "scheduler" not in value:
                continue
            scheduler = value["scheduler"] or {}
            next_run = scheduler.get("next_run")
            results.append(
                {
                    "task": key,
                    "enable": bool(scheduler.get("enable")),
                    "next_run": str(next_run) if next_run is not None else None,
                    "priority": scheduler.get("priority"),
                    "success_interval": str(scheduler.get("success_interval")),
                    "failure_interval": str(scheduler.get("failure_interval")),
                }
            )
        return results

    def task_args(self, name: str, task: str) -> dict:
        return self._config(name).script_task(task)

    def log(self, name: str, cursor: int | None = None, limit: int = 200) -> dict:
        self._process(name)
        return self.tailer.read(name, cursor=cursor, limit=limit)

    def screenshot(self, name: str) -> dict:
        self._process(name)
        payload, ts = self.frame_store.latest()
        age = self.frame_store.age()
        return {
            "available": payload is not None,
            "image": payload,
            "captured_at": ts,
            "age_s": round(age, 1) if age is not None else None,
        }

    def briefing(self) -> dict:
        playbook = ""
        path = Path("docs/agent/daily-playbook.md")
        if path.exists():
            playbook = path.read_text(encoding="utf-8")[:4000]
        return {
            "now": self._now().strftime("%Y-%m-%dT%H:%M:%S"),
            "instances": self.list_instances(),
            "plan": self.memory.plan(),
            "state": self.memory.state(),
            "hold": self._hold_dict(),
            "playbook": playbook,
        }
