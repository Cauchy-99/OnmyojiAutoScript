# This Python file uses the following encoding: utf-8
"""data/agent 下的计划、状态、日记、issue 与审批。"""
from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

from module.config.atomicwrites import atomic_write
from module.server.agent.policy import PROTECTED_DEFAULT

DEFAULT_PLAN: dict[str, Any] = {
    "timezone": "Asia/Shanghai",
    "wake": ["07:00", "12:00", "18:00", "22:30"],
    "must": [],
    "optional": [],
    "protected_tasks": list(PROTECTED_DEFAULT),
    "budget": {"max_active_hours": 10, "quiet_hours": []},
    "on_failure": {"retry": 2, "then": "Restart", "then_notify": True, "skip": True},
}

_WAKE_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_STATUSES = ("open", "resolved", "wontfix")


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class AgentMemory:
    def __init__(
        self,
        root: str | Path = "data/agent",
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.root = Path(root)
        self.issues_dir = self.root / "issues"
        self.journal_dir = self.root / "journal"
        self.workspace_dir = self.root / "workspace"
        for folder in (self.root, self.issues_dir, self.journal_dir, self.workspace_dir):
            folder.mkdir(parents=True, exist_ok=True)
        self._now = now

    # ---------------------------------------------------------------- plan
    def plan(self) -> dict:
        path = self.root / "daily_plan.yaml"
        loaded: dict = {}
        if path.exists():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise ValueError("daily_plan.yaml must be a mapping")
            loaded = raw

        plan = _deep_merge(DEFAULT_PLAN, loaded)
        wake = plan.get("wake") or []
        if not isinstance(wake, list) or not all(
            isinstance(item, str) and _WAKE_PATTERN.match(item) for item in wake
        ):
            raise ValueError("wake must be a list of HH:MM strings")
        for key in ("must", "optional", "protected_tasks"):
            if not isinstance(plan.get(key), list):
                raise ValueError(f"{key} must be a list")
        retry = plan["on_failure"].get("retry")
        if not isinstance(retry, int) or retry < 0:
            raise ValueError("on_failure.retry must be a non-negative int")
        return plan

    # --------------------------------------------------------------- state
    def state(self, instance: str | None = None) -> dict:
        data = self._load_state()
        if instance is None:
            return data
        return self._instance_state(data, instance)

    def update_state(self, patch: dict, instance: str | None = None) -> dict:
        data = self._load_state()
        if instance is None:
            data = merged = _deep_merge(data, patch)
        else:
            bucket = self._instance_state(data, instance)
            data.setdefault("instances", {})[instance] = merged = _deep_merge(bucket, patch)
        self._write_json(self.root / "state.json", data)
        return merged

    def _load_state(self) -> dict:
        path = self.root / "state.json"
        today = self._now().date().isoformat()
        if not path.exists():
            return {"date": today, "instances": {}}
        data = json.loads(path.read_text(encoding="utf-8"))
        legacy = any(key in data for key in ("done", "skipped", "attempts"))
        dirty = legacy
        if legacy:
            # 折叠旧扁平键；若文件已同时有 instances，保留其它实例，只覆盖 default
            instances = data.get("instances") or {}
            instances["default"] = {
                "done": data.pop("done", []),
                "skipped": data.pop("skipped", []),
                "attempts": data.pop("attempts", {}),
            }
            data = {"date": data.get("date", today), "instances": instances}
        if data.get("date") != today:
            self.append_journal(f"跨天：{data.get('date')} -> {today}，重置当日进度")
            data = {"date": today, "instances": {}}
            dirty = True
        if "instances" not in data:
            data["instances"] = {}
            dirty = True
        if dirty:
            self._write_json(path, data)
        return data

    @staticmethod
    def _instance_state(data: dict, instance: str) -> dict:
        return data.setdefault("instances", {}).setdefault(
            instance, {"done": [], "skipped": [], "attempts": {}}
        )

    # ------------------------------------------------------------- journal
    def append_journal(self, text: str) -> str:
        now = self._now()
        path = self.journal_dir / f"{now.date().isoformat()}.md"
        line = f"- {now.strftime('%H:%M')} {text}\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        return str(path)

    # -------------------------------------------------------------- issues
    def file_issue(
        self,
        task: str,
        symptom: str,
        suggestion: str = "",
        log_excerpt: str = "",
        snapshot: dict | None = None,
        frame: bytes | None = None,
    ) -> dict:
        now = self._now()
        slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-") or "task"
        issue_id = f"ISS-{now.strftime('%Y%m%d-%H%M%S')}-{slug}"
        body = {
            "id": issue_id,
            "created_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "task": task,
            "phase": "run",
            "status": "open",
            "symptom": symptom,
            "suggestion": suggestion,
            "snapshot": snapshot or {},
            "log_excerpt": log_excerpt[-8000:],
            "screenshot": f"{issue_id}.jpg" if frame else None,
        }
        path = self.issues_dir / f"{issue_id}.md"
        front = yaml.safe_dump(
            {key: body[key] for key in
             ("id", "created_at", "task", "phase", "status", "screenshot")},
            allow_unicode=True,
            sort_keys=False,
        ).strip()
        text = (
            f"---\n{front}\n---\n\n"
            f"## 症状\n{symptom}\n\n"
            f"## 当时的快照\n```json\n{json.dumps(body['snapshot'], ensure_ascii=False, indent=2)}\n```\n\n"
            f"## 日志片段\n```\n{body['log_excerpt']}\n```\n\n"
            f"## 复现建议\n{suggestion or '（无）'}\n"
        )
        with atomic_write(str(path), overwrite=True, encoding="utf-8") as handle:
            handle.write(text)
        if frame:
            (self.issues_dir / f"{issue_id}.jpg").write_bytes(frame)
        return body

    def list_issues(self, status: str | None = None) -> list[dict]:
        issues = []
        for path in sorted(self.issues_dir.glob("ISS-*.md")):
            meta = self._read_front_matter(path)
            if not meta:
                continue
            if status and meta.get("status") != status:
                continue
            meta["path"] = str(path)
            issues.append(meta)
        return issues

    def set_issue_status(self, issue_id: str, status: str) -> bool:
        if status not in _STATUSES:
            raise ValueError(f"status must be one of {_STATUSES}")
        if not re.fullmatch(r"ISS-\d{8}-\d{6}-[a-z0-9-]+", issue_id):
            raise ValueError(f"invalid issue id: {issue_id}")
        path = self.issues_dir / f"{issue_id}.md"
        if not path.exists():
            return False
        text = path.read_text(encoding="utf-8")
        updated = re.sub(r"^status: .*$", f"status: {status}", text, count=1, flags=re.MULTILINE)
        if updated == text:
            return False
        with atomic_write(str(path), overwrite=True, encoding="utf-8") as handle:
            handle.write(updated)
        return True

    # ----------------------------------------------------------- approvals
    def approvals(self) -> dict:
        path = self.root / "approvals.json"
        if not path.exists():
            return {"pending": [], "tokens": []}
        return json.loads(path.read_text(encoding="utf-8"))

    def add_approval(self, action: dict) -> dict:
        data = self.approvals()
        approval = {
            "token": uuid.uuid4().hex[:12],
            "created_at": self._now().strftime("%Y-%m-%dT%H:%M:%S"),
            "action": action,
        }
        data.setdefault("pending", []).append(approval)
        self._write_json(self.root / "approvals.json", data)
        return approval

    def grant_approval(self, token: str) -> bool:
        """人工批准：把 pending 里的请求移进可用列表，保留它批准的动作。"""
        data = self.approvals()
        pending = data.get("pending", [])
        entry = next((item for item in pending if item.get("token") == token), None)
        if entry is None:
            return False
        pending.remove(entry)
        tokens = data.setdefault("tokens", [])
        if not any(
            (item.get("token") if isinstance(item, dict) else item) == token
            for item in tokens
        ):
            tokens.append({"token": token, "action": entry.get("action") or {}})
        self._write_json(self.root / "approvals.json", data)
        return True

    def check_approval(self, token: str, kind: str, task: str | None) -> bool:
        """只校验、不消耗：令牌存在且批准的动作与本次写操作一致。"""
        data = self.approvals()
        for item in data.get("tokens", []):
            if isinstance(item, str):
                if item == token:
                    return True
                continue
            if item.get("token") != token:
                continue
            action = item.get("action") or {}
            return action.get("kind") == kind and action.get("task") == task
        return False

    def consume_approval(self, token: str) -> bool:
        data = self.approvals()
        tokens = data.setdefault("tokens", [])
        for index, item in enumerate(tokens):
            current = item if isinstance(item, str) else item.get("token")
            if current == token:
                tokens.pop(index)
                self._write_json(self.root / "approvals.json", data)
                return True
        return False

    # -------------------------------------------------------------- helper
    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        with atomic_write(str(path), overwrite=True, encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2))

    @staticmethod
    def _read_front_matter(path: Path) -> dict:
        text = path.read_text(encoding="utf-8")
        match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        if not match:
            return {}
        return yaml.safe_load(match.group(1)) or {}
