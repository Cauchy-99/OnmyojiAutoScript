# This Python file uses the following encoding: utf-8
"""agent 能力层：REST 与 MCP 共用的业务函数。"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
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
from module.server.agent.policy import ActionClass, HoldActive, HoldGate, NeedsApproval, classify_write


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
        for name, process in list(processes.items()):
            running_task = ""
            try:
                running_task = self._config(name).model.dict().get("running_task", "") or ""
            except Exception:
                running_task = ""
            age = self.frame_store.age(name)
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
            pending = [
                {"name": item.command, "next_run": str(item.next_run)}
                for item in (getattr(config, "pending_task", None) or [])
            ]
            waiting = [
                {"name": item.command, "next_run": str(item.next_run)}
                for item in (getattr(config, "waiting_task", None) or [])
            ]
            running: dict = {}
            running_task = config.model.dict().get("running_task", "") or ""
            if running_task:
                from module.config.utils import convert_to_underscore

                key = convert_to_underscore(running_task)
                match = next((item for item in pending if item["name"] == key), None)
                if match is not None:
                    pending.remove(match)
                    running = match
                else:
                    running = {"name": key, "next_run": None}
            schedule = {"running": running, "pending": pending, "waiting": waiting}
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
                    "success_interval": None if scheduler.get("success_interval") is None else str(scheduler.get("success_interval")),
                    "failure_interval": None if scheduler.get("failure_interval") is None else str(scheduler.get("failure_interval")),
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
        payload, ts = self.frame_store.latest(name)
        age = None if ts is None else max(0.0, time.time() - ts)
        return {
            "available": payload is not None,
            "image": payload,
            "captured_at": ts,
            "age_s": round(age, 1) if age is not None else None,
        }

    def briefing(self, instance: str | None = None) -> dict:
        playbook = ""
        path = Path("docs/agent/daily-playbook.md")
        if path.exists():
            playbook = path.read_text(encoding="utf-8")[:4000]
        state = self.memory.state(instance)
        if instance is None:
            # 兼容只读旧字段的调用方：整份 state 文档仍返回，同时镜像 default 桶的旧扁平键
            default = state.get("instances", {}).get("default", {})
            state = {
                **state,
                "done": list(default.get("done", [])),
                "skipped": list(default.get("skipped", [])),
                "attempts": dict(default.get("attempts", {})),
            }
        return {
            "now": self._now().strftime("%Y-%m-%dT%H:%M:%S"),
            "instances": self.list_instances(),
            "plan": self.memory.plan(),
            "state": state,
            "hold": self._hold_dict(),
            "playbook": playbook,
        }

    # --------------------------------------------------------- guard rails
    def _guard(self, kind: str, task: str | None = None, approval_token: str | None = None) -> None:
        hold = self._hold.state()
        if hold.active and kind not in {"hold", "release_hold", "estop"}:
            raise HoldActive("hold is active; writes are disabled", hold.to_dict())

        if kind in {"hold", "release_hold", "estop"}:
            # 急停/让位不能被计划文件的语法错误挡住
            return

        protected = self.memory.plan().get("protected_tasks") or ()
        if classify_write(kind, task, protected) is not ActionClass.IRREVERSIBLE:
            return

        if approval_token and self.memory.check_approval(approval_token, kind, task):
            return

        approval = self.memory.add_approval(
            {"kind": kind, "task": task, "requested_at": self._now().strftime("%Y-%m-%dT%H:%M:%S")}
        )
        raise NeedsApproval(
            f"{task} is protected; human approval required",
            {"task": task, "kind": kind, "approval_token": approval["token"]},
        )

    def _write(
        self,
        name: str,
        kind: str,
        apply_fn: Callable[[Any], bool],
        verify_fn: Callable[[Any], bool],
        task: str | None = None,
        approval_token: str | None = None,
        effective: str = EFFECTIVE_NEXT_BOUNDARY,
        detail: dict | None = None,
    ) -> dict:
        self._guard(kind, task, approval_token)
        config = self._config(name)
        if not apply_fn(config):
            raise ValueError(f"apply failed: kind={kind} task={task}")

        fresh = self._config(name)
        verdict = verify_fn(fresh)
        if verdict is False:
            retry_config = self._config(name)
            apply_fn(retry_config)
            fresh = self._config(name)
            verdict = verify_fn(fresh)
            if verdict is False:
                self.memory.append_journal(f"写入校验失败 kind={kind} task={task}")
                raise ValueError(f"verify failed: kind={kind} task={task}")
        verified = verdict is True

        if approval_token:
            self.memory.consume_approval(approval_token)

        payload = dict(detail or {})
        payload.setdefault("instance", name)
        payload.setdefault("verified", verified)
        if task:
            payload.setdefault("task", task)
        return WriteResult(
            ok=True, applied_at=self._now(), effective=effective, detail=payload
        ).to_dict()

    @staticmethod
    def _normalize_task(task: str) -> str:
        from module.config.utils import convert_to_underscore

        return convert_to_underscore(task)

    def _require_task(self, config: Any, task: str) -> str:
        key = self._normalize_task(task)
        data = config.model.dict()
        if key not in data or not isinstance(data[key], dict) or "scheduler" not in data[key]:
            raise ValueError(f"unknown task: {task}")
        return key

    @staticmethod
    def _parse_at(at: str) -> str:
        try:
            parsed = datetime.strptime(at, "%Y-%m-%d %H:%M:%S")
        except ValueError as error:
            raise ValueError("at must be 'YYYY-MM-DD HH:MM:SS'") from error
        return parsed.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _same_value(stored: Any, value: Any) -> bool:
        """比较写入值与 pydantic 转换后的存储值。"""
        if stored == value:
            return True
        if isinstance(stored, bool) or isinstance(value, bool):
            return str(stored).strip().lower() == str(value).strip().lower()
        return str(stored) == str(value)

    # --------------------------------------------------------------- write
    def set_task_enabled(
        self, name: str, task: str, enabled: bool, approval_token: str | None = None
    ) -> dict:
        key = self._normalize_task(task)

        def apply(config):
            self._require_task(config, key)
            return config.script_set_arg(key, "scheduler", "enable", bool(enabled))

        def verify(config):
            return bool(config.model.dict()[key]["scheduler"]["enable"]) == bool(enabled)

        return self._write(name, "task_enable", apply, verify, task=key,
                           approval_token=approval_token)

    def set_task_arg(
        self,
        name: str,
        task: str,
        group: str,
        argument: str,
        value: Any,
        approval_token: str | None = None,
    ) -> dict:
        key = self._normalize_task(task)

        def apply(config):
            self._require_task(config, key)
            return config.script_set_arg(key, group, argument, value)

        def verify(config):
            data = config.model.dict()
            node = data.get(key) or {}
            group_node = node.get(group)
            if not isinstance(group_node, dict) or argument not in group_node:
                return None
            return self._same_value(group_node.get(argument), value)

        return self._write(
            name, "task_args", apply, verify, task=key, approval_token=approval_token,
            detail={"group": group, "argument": argument, "value": value},
        )

    def set_next_run(
        self, name: str, task: str, at: str, approval_token: str | None = None
    ) -> dict:
        key = self._normalize_task(task)
        stamp = self._parse_at(at)

        def apply(config):
            self._require_task(config, key)
            return config.script_set_arg(key, "scheduler", "next_run", stamp)

        def verify(config):
            return str(config.model.dict()[key]["scheduler"]["next_run"]) == stamp

        return self._write(
            name, "next_run", apply, verify, task=key, approval_token=approval_token,
            detail={"next_run": stamp},
        )

    async def run_now(
        self, name: str, task: str, position: str = "front", approval_token: str | None = None
    ) -> dict:
        if position not in {"front", "end"}:
            raise ValueError("position must be 'front' or 'end'")
        key = self._normalize_task(task)
        config = self._config(name)
        self._require_task(config, key)

        now = self._now()
        target = now if position == "front" else now + timedelta(minutes=5)
        stamp = target.strftime("%Y-%m-%d %H:%M:%S")

        def apply(cfg):
            return cfg.script_set_arg(key, "scheduler", "next_run", stamp)

        result = self._write(
            name, "run_now", apply, lambda cfg: str(cfg.model.dict()[key]["scheduler"]["next_run"]) == stamp,
            task=key, approval_token=approval_token, detail={"next_run": stamp, "position": position},
        )

        process = self._process(name)
        if self._state_name(process) != "RUNNING":
            await process.start()
            result["detail"]["started"] = True
        return result

    def set_schedule_rule(self, name: str, rule: str) -> dict:
        allowed = {"Filter", "FIFO", "Priority"}
        if rule not in allowed:
            raise ValueError(f"rule must be one of {sorted(allowed)}")

        def apply(config):
            return config.script_set_arg("script", "optimization", "schedule_rule", rule)

        def verify(config):
            value = config.model.dict()["script"]["optimization"]["schedule_rule"]
            return getattr(value, "value", value) == rule

        return self._write(name, "schedule_rule", apply, verify, detail={"rule": rule})

    def send_command(self, name: str, command: str) -> dict:
        self._guard("command")
        self._process(name)
        self.commander.send(name, command)
        return WriteResult(
            ok=True, applied_at=self._now(), effective=EFFECTIVE_NEXT_BOUNDARY,
            detail={"instance": name, "command": command},
        ).to_dict()

    async def set_running(self, name: str, running: bool) -> dict:
        process = self._process(name)
        if running:
            self._guard("instance")
        if running:
            if self._state_name(process) != "RUNNING":
                await process.start()
        else:
            await process.stop()
        return WriteResult(
            ok=True, applied_at=self._now(), effective=EFFECTIVE_IMMEDIATE,
            detail={"instance": name, "running": bool(running)},
        ).to_dict()

    async def emergency_stop(self, minutes: int = 120) -> dict:
        if minutes <= 0:
            raise ValueError("minutes must be positive")
        self._guard("estop")
        # 先关闸门（fail-safe），停完之后再刷新窗口，让返回的窗口从此刻起算
        self._hold.activate(minutes=minutes, reason="emergency_stop")

        processes = getattr(self.manager, "script_process", {}) or {}
        stopped: list[str] = []
        failed: list[str] = []
        for name, process in list(processes.items()):
            try:
                await process.stop()
                stopped.append(name)
            except Exception:
                failed.append(name)

        self.memory.append_journal(
            f"急停：停止 {', '.join(stopped) or '无实例'}，失败 {', '.join(failed) or '无'}，让位 {minutes} 分钟"
        )
        hold = self._hold.activate(minutes=minutes, reason="emergency_stop")
        return {"ok": not failed, "stopped": stopped, "failed": failed, "hold": hold.to_dict()}

    # ------------------------------------------------------- hold & memory
    def hold(self, minutes: int = 120, reason: str = "") -> dict:
        state = self._hold.activate(minutes=minutes, reason=reason)
        self.memory.append_journal(f"让位 {minutes} 分钟：{reason or '未说明'}")
        return {"ok": True, "hold": state.to_dict()}

    def release_hold(self) -> dict:
        state = self._hold.release()
        self.memory.append_journal("结束让位")
        return {"ok": True, "hold": state.to_dict()}

    def update_daily_state(self, patch: dict, instance: str | None = None) -> dict:
        return self.memory.update_state(patch, instance)

    def journal(self, text: str) -> dict:
        return {"ok": True, "path": self.memory.append_journal(text)}

    def list_issues(self, status: str | None = None) -> list[dict]:
        return self.memory.list_issues(status=status)

    def file_issue(
        self,
        task: str,
        symptom: str,
        suggestion: str = "",
        instance: str | None = None,
        include_log: int = 80,
    ) -> dict:
        name = instance or next(iter(getattr(self.manager, "script_process", {}) or {}), None)
        snapshot = {}
        log_excerpt = ""
        frame = None
        if name:
            try:
                snapshot = self.snapshot(name)
                log_excerpt = "\n".join(self.log(name, limit=include_log)["lines"])
            except Exception:
                pass
            payload, _ = self.frame_store.latest(name)
            frame = payload
        return self.memory.file_issue(
            task=task,
            symptom=symptom,
            suggestion=suggestion,
            log_excerpt=log_excerpt,
            snapshot=snapshot,
            frame=frame,
        )
