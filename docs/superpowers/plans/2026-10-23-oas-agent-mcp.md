# OAS Agent MCP 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 OAS 后端里加一层 agent 能力层，同时用 REST `/api/agent/*` 和 MCP `/mcp` 两个入口暴露「看状态、排任务、启停实例、截图、记 issue」的能力，让 agent 每天自动刷任务、刷不过就跳过。

**Architecture:** 业务函数只写一遍（`module/server/agent/`），REST 与 MCP 是同一批函数的两个薄壳。设备操作通过 `multiprocessing.Queue` 回到持有 `Device` 的脚本子进程，截图通过被动帧缓存回传，不跨线程碰 `Device`。所有写操作先过 `policy.py` 的 hold/分级闸门。

**Tech Stack:** Python 3.10+、FastAPI（仓库固定 0.104.1）、pydantic 2、`mcp==1.27.1`（FastMCP，streamable-http）、pytest、multiprocessing。

## Global Constraints

- 目标运行时 Python 3.10（仓库 `requirements.txt` 由 3.10 编译），本机解释器是 3.13.7；写代码不得使用 3.11+ 独有语法（不要 `ExceptionGroup`、`typing.Self`、`StrEnum`）。
- 只新增一个运行时依赖：`mcp==1.27.1`（本机已装 1.27.1）。
- 新增代码只放在 `module/server/agent/`、`module/server/agent_router.py`、`module/server/mcp_server.py`、`tests/agent/`、`docs/agent/`、`scripts/agent_smoke.py`、`config/agent/daily_plan.example.yaml`；对既有文件的改动仅限 `module/server/app.py`、`module/server/script_process.py`、`script.py`、`requirements-in.txt`、`.gitignore`。
- 不许改 `tasks/` 下任何任务实现，不许改 OASX。
- `/api/agent/*` 与 `/mcp` 只接受回环地址（`127.0.0.1`、`::1`、`localhost`）。
- 所有 agent 写操作经过 `policy.classify_write`，hold 生效时一律拒绝。
- 测试从仓库根运行：`python -m pytest tests/agent -v`。
- 每个 Task 结束必须提交一次。

---

### Task 1: 数据契约与策略闸门

**Files:**
- Create: `module/server/agent/__init__.py`
- Create: `module/server/agent/models.py`
- Create: `module/server/agent/policy.py`
- Test: `tests/agent/test_policy.py`

**Interfaces:**
- Consumes: `module.config.utils.convert_to_underscore`
- Produces:
  - `models.HoldState(active: bool, until: datetime|None, reason: str)` + `.to_dict()`
  - `models.InstanceView(name, state, pid_alive, running_task, frame_age_s, hold_active)` + `.to_dict()`
  - `models.Snapshot(name, state, running_task, schedule, hold)` + `.to_dict()`
  - `models.WriteResult(ok, applied_at, effective, detail)` + `.to_dict()`
  - `models.EFFECTIVE_IMMEDIATE = "immediate"`、`models.EFFECTIVE_NEXT_BOUNDARY = "next_task_boundary"`
  - `policy.PolicyDenied(reason, detail)`、`policy.HoldActive`、`policy.NeedsApproval`
  - `policy.PROTECTED_DEFAULT: tuple[str, ...]`
  - `policy.classify_write(kind: str, task: str|None = None, protected: Sequence[str] = PROTECTED_DEFAULT) -> str`
  - `policy.HoldGate(now: Callable[[], datetime])`，方法 `activate(minutes, reason="")`、`release()`、`state() -> HoldState`

- [ ] **Step 1: 写失败测试**

创建 `tests/agent/__init__.py`（空文件）与 `tests/agent/test_policy.py`：

```python
import pytest
from datetime import datetime, timedelta

from module.server.agent import models, policy


def test_classify_read_kinds_are_not_writes():
    with pytest.raises(ValueError):
        policy.classify_write("read_something")


def test_classify_plain_task_write_is_reversible():
    assert policy.classify_write("task_enable", "Exploration") == policy.ActionClass.REVERSIBLE


def test_classify_protected_task_write_needs_approval():
    assert policy.classify_write("task_args", "MysteryShop") == policy.ActionClass.IRREVERSIBLE


def test_classify_is_case_and_style_insensitive():
    assert policy.classify_write("task_args", "mysteryShop") == policy.ActionClass.IRREVERSIBLE


def test_instance_control_is_reversible():
    assert policy.classify_write("instance") == policy.ActionClass.REVERSIBLE


def test_task_scoped_kind_requires_task():
    for kind in ("task_enable", "task_args", "next_run", "run_now"):
        with pytest.raises(ValueError):
            policy.classify_write(kind)


def test_uppercase_protected_task_name_is_still_protected():
    assert policy.classify_write("task_enable", "MYSTERY_SHOP") == policy.ActionClass.IRREVERSIBLE


def test_hold_activates_and_expires():
    now = datetime(2026, 10, 23, 12, 0, 0)

    class Clock:
        current = now

        def __call__(self):
            return self.current

    clock = Clock()
    gate = policy.HoldGate(clock)
    assert gate.state().active is False

    gate.activate(minutes=30, reason="playing with friends")
    state = gate.state()
    assert state.active is True
    assert state.until == now + timedelta(minutes=30)

    clock.current = now + timedelta(minutes=31)
    assert gate.state().active is False


def test_hold_release_is_immediate():
    gate = policy.HoldGate(lambda: datetime(2026, 10, 23, 12, 0, 0))
    gate.activate(minutes=120)
    gate.release()
    assert gate.state().active is False


def test_write_result_serialises_datetime():
    result = models.WriteResult(
        ok=True,
        applied_at=datetime(2026, 10, 23, 18, 15, 0),
        effective=models.EFFECTIVE_NEXT_BOUNDARY,
        detail={"task": "Orochi"},
    )
    payload = result.to_dict()
    assert payload["applied_at"] == "2026-10-23T18:15:00"
    assert payload["effective"] == "next_task_boundary"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/agent/test_policy.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'module.server.agent'`

- [ ] **Step 3: 写 `models.py`**

创建 `module/server/agent/__init__.py`（空文件），再创建 `module/server/agent/models.py`：

```python
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
```

- [ ] **Step 4: 写 `policy.py`**

创建 `module/server/agent/policy.py`：

```python
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
            # 任务级写操作必须指明任务，缺失即拒绝（fail-closed）
            raise ValueError(f"action kind `{kind}` requires a task")
        protected_set = {convert_to_underscore(item).lower() for item in protected}
        if convert_to_underscore(task).lower() in protected_set:
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
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/agent/test_policy.py -v`
Expected: PASS，10 passed

- [ ] **Step 6: 提交**

```bash
git add module/server/agent/__init__.py module/server/agent/models.py module/server/agent/policy.py tests/agent/__init__.py tests/agent/test_policy.py
git commit -m "feat(agent): data contracts and policy gate"
```

---

### Task 2: 日志增量读取

**Files:**
- Create: `module/server/agent/logtail.py`
- Test: `tests/agent/test_logtail.py`

**Interfaces:**
- Consumes: 无
- Produces: `logtail.LogTailer(root: str|Path = "log", date_fn: Callable[[], date] = date.today)`，
  方法 `path(name: str) -> Path`、`read(name: str, cursor: int|None = None, limit: int = 200) -> dict`，
  返回 `{"lines": list[str], "cursor": int, "eof": bool, "path": str}`；`cursor` 是字节偏移。

- [ ] **Step 1: 写失败测试**

创建 `tests/agent/test_logtail.py`：

```python
from datetime import date

from module.server.agent.logtail import LogTailer


def _tailer(tmp_path):
    return LogTailer(root=tmp_path, date_fn=lambda: date(2026, 10, 23))


def test_missing_log_returns_empty(tmp_path):
    result = _tailer(tmp_path).read("oas1")
    assert result["lines"] == []
    assert result["eof"] is True
    assert result["cursor"] == 0


def test_tail_without_cursor_returns_last_lines(tmp_path):
    log = tmp_path / "2026-10-23_oas1.txt"
    log.write_text("\n".join(f"line{i}" for i in range(10)), encoding="utf-8")

    result = _tailer(tmp_path).read("oas1", limit=3)
    assert result["lines"] == ["line7", "line8", "line9"]
    assert result["eof"] is True


def test_cursor_continuation_returns_only_new_lines(tmp_path):
    log = tmp_path / "2026-10-23_oas1.txt"
    log.write_text("a\nb\n", encoding="utf-8")
    tailer = _tailer(tmp_path)

    first = tailer.read("oas1")
    assert first["lines"] == ["a", "b"]

    with log.open("a", encoding="utf-8") as handle:
        handle.write("c\nd\n")

    second = tailer.read("oas1", cursor=first["cursor"])
    assert second["lines"] == ["c", "d"]
    assert second["cursor"] > first["cursor"]


def test_cursor_beyond_file_restarts_from_tail(tmp_path):
    log = tmp_path / "2026-10-23_oas1.txt"
    log.write_text("only\n", encoding="utf-8")
    result = _tailer(tmp_path).read("oas1", cursor=10_000)
    assert result["lines"] == ["only"]


def test_utf8_multibyte_is_not_split(tmp_path):
    log = tmp_path / "2026-10-23_oas1.txt"
    log.write_text("任务开始\n任务结束\n", encoding="utf-8")
    tailer = _tailer(tmp_path)
    result = tailer.read("oas1", limit=1)
    assert result["lines"] == ["任务结束"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/agent/test_logtail.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'module.server.agent.logtail'`

- [ ] **Step 3: 写实现**

创建 `module/server/agent/logtail.py`：

```python
# This Python file uses the following encoding: utf-8
"""增量读取 ./log/<date>_<name>.txt。"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Callable

_BYTES_PER_LINE_GUESS = 400


class LogTailer:
    def __init__(
        self,
        root: str | Path = "log",
        date_fn: Callable[[], date] = date.today,
    ) -> None:
        self.root = Path(root)
        self._date_fn = date_fn

    def path(self, name: str) -> Path:
        return self.root / f"{self._date_fn().isoformat()}_{name}.txt"

    def read(self, name: str, cursor: int | None = None, limit: int = 200) -> dict:
        if limit <= 0:
            raise ValueError("limit must be positive")
        path = self.path(name)
        if not path.exists():
            return {"lines": [], "cursor": 0, "eof": True, "path": str(path)}

        data = path.read_bytes()
        size = len(data)
        if cursor is None or cursor < 0 or cursor > size:
            start = max(0, size - limit * _BYTES_PER_LINE_GUESS)
            trimmed = True
        else:
            start = cursor
            trimmed = False

        text = data[start:].decode("utf-8", errors="replace")
        lines = text.splitlines()
        if trimmed and len(lines) > limit:
            lines = lines[-limit:]
        if not trimmed and lines and lines[0] == "":
            lines = lines[1:]

        return {"lines": lines, "cursor": size, "eof": True, "path": str(path)}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/agent/test_logtail.py -v`
Expected: PASS，5 passed

- [ ] **Step 5: 提交**

```bash
git add module/server/agent/logtail.py tests/agent/test_logtail.py
git commit -m "feat(agent): incremental log tailer"
```

---

### Task 3: 每日记忆、issue 与默认计划

**Files:**
- Create: `module/server/agent/memory.py`
- Create: `config/agent/daily_plan.example.yaml`
- Modify: `.gitignore`（追加 `data/agent/`）
- Test: `tests/agent/test_memory.py`

**Interfaces:**
- Consumes: `module.server.agent.policy.PROTECTED_DEFAULT`
- Produces:
  - `memory.DEFAULT_PLAN: dict`
  - `memory.AgentMemory(root: str|Path = "data/agent", now: Callable[[], datetime] = datetime.now)`
  - 方法：`plan() -> dict`、`state() -> dict`、`update_state(patch: dict) -> dict`、
    `append_journal(text: str) -> str`、`file_issue(task: str, symptom: str, suggestion: str = "", log_excerpt: str = "", snapshot: dict|None = None, frame: bytes|None = None) -> dict`、
    `list_issues(status: str|None = None) -> list[dict]`、`set_issue_status(issue_id: str, status: str) -> bool`、
    `approvals() -> dict`、`add_approval(action: dict) -> dict`、`consume_approval(token: str) -> bool`

- [ ] **Step 1: 写失败测试**

创建 `tests/agent/test_memory.py`：

```python
import json
from datetime import datetime

from module.server.agent.memory import AgentMemory

FIXED_NOW = datetime(2026, 10, 23, 18, 15, 0)


def _memory(tmp_path):
    return AgentMemory(root=tmp_path, now=lambda: FIXED_NOW)


def test_plan_defaults_when_file_missing(tmp_path):
    plan = _memory(tmp_path).plan()
    assert plan["timezone"] == "Asia/Shanghai"
    assert plan["on_failure"]["retry"] == 2
    assert "mystery_shop" in plan["protected_tasks"]


def test_plan_reads_yaml_and_keeps_defaults(tmp_path):
    (tmp_path / "daily_plan.yaml").write_text(
        "must: [Exploration]\nbudget:\n  max_active_hours: 4\n", encoding="utf-8"
    )
    plan = _memory(tmp_path).plan()
    assert plan["must"] == ["Exploration"]
    assert plan["budget"]["max_active_hours"] == 4
    assert plan["budget"]["quiet_hours"] == []


def test_plan_rejects_bad_wake_time(tmp_path):
    (tmp_path / "daily_plan.yaml").write_text("wake: ['7:00']\n", encoding="utf-8")
    try:
        _memory(tmp_path).plan()
    except ValueError as error:
        assert "wake" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_state_patch_merges_and_persists(tmp_path):
    memory = _memory(tmp_path)
    memory.update_state({"date": "2026-10-23", "done": ["Restart"]})
    merged = memory.update_state({"done": ["Restart", "WantedQuests"], "attempts": {"Orochi": 1}})
    assert merged["done"] == ["Restart", "WantedQuests"]
    on_disk = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert on_disk["attempts"] == {"Orochi": 1}


def test_journal_appends_one_line_per_call(tmp_path):
    memory = _memory(tmp_path)
    path = memory.append_journal("插队 Orochi")
    memory.append_journal("跳过 Orochi")
    lines = open(path, encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 2
    assert "18:15" in lines[0]
    assert "插队 Orochi" in lines[0]


def test_issue_roundtrip(tmp_path):
    memory = _memory(tmp_path)
    issue = memory.file_issue(
        task="Orochi",
        symptom="连续失败 2 次",
        suggestion="检查御魂队伍",
        log_excerpt="ERROR boom",
        snapshot={"state": "RUNNING"},
    )
    assert issue["id"].startswith("ISS-20261023-181500")
    assert issue["status"] == "open"

    listed = memory.list_issues()
    assert len(listed) == 1
    assert listed[0]["task"] == "Orochi"

    assert memory.set_issue_status(issue["id"], "resolved") is True
    assert memory.list_issues(status="resolved")[0]["id"] == issue["id"]
    assert memory.list_issues(status="open") == []


def test_issue_writes_screenshot_when_frame_given(tmp_path):
    memory = _memory(tmp_path)
    issue = memory.file_issue(task="Orochi", symptom="stuck", frame=b"\xff\xd8jpeg")
    assert (tmp_path / "issues" / f"{issue['id']}.jpg").exists()


def test_approval_token_requires_human_grant(tmp_path):
    memory = _memory(tmp_path)
    approval = memory.add_approval({"kind": "task_args", "task": "MysteryShop"})
    token = approval["token"]

    assert memory.consume_approval(token) is False
    assert memory.grant_approval(token) is True
    assert memory.consume_approval(token) is True
    assert memory.consume_approval(token) is False
    assert memory.grant_approval(token) is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/agent/test_memory.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'module.server.agent.memory'`

- [ ] **Step 3: 写实现**

创建 `module/server/agent/memory.py`：

```python
# This Python file uses the following encoding: utf-8
"""data/agent 下的计划、状态、日记、issue 与审批。"""
from __future__ import annotations

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
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
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
    def state(self) -> dict:
        path = self.root / "state.json"
        if not path.exists():
            return {"date": self._now().date().isoformat(), "done": [], "skipped": [], "attempts": {}}
        return json.loads(path.read_text(encoding="utf-8"))

    def update_state(self, patch: dict) -> dict:
        merged = _deep_merge(self.state(), patch)
        self._write_json(self.root / "state.json", merged)
        return merged

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
        path = self.issues_dir / f"{issue_id}.md"
        if not path.exists():
            return False
        text = path.read_text(encoding="utf-8")
        updated = re.sub(r"^status: .*$", f"status: {status}", text, count=1, flags=re.MULTILINE)
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
        """人工批准：把 pending 里的请求移进可用列表。不通过任何 agent 工具暴露。"""
        data = self.approvals()
        pending = data.get("pending", [])
        entry = next((item for item in pending if item.get("token") == token), None)
        if entry is None:
            return False
        pending.remove(entry)
        tokens = data.setdefault("tokens", [])
        if token not in tokens:
            tokens.append(token)
        self._write_json(self.root / "approvals.json", data)
        return True

    def consume_approval(self, token: str) -> bool:
        data = self.approvals()
        tokens = data.setdefault("tokens", [])
        if token not in tokens:
            return False
        tokens.remove(token)
        self._write_json(self.root / "approvals.json", data)
        return True

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
```

- [ ] **Step 4: 写默认计划样例**

创建 `config/agent/daily_plan.example.yaml`：

```yaml
# 复制到 data/agent/daily_plan.yaml 后按需修改。
timezone: Asia/Shanghai
wake: ["07:00", "12:00", "18:00", "22:30"]
must: [Exploration, WantedQuests, SoulsTidy]
optional: [KittyShop, MysteryShop]
protected_tasks: [MysteryShop, RichMan, TalismanPass]
budget:
  max_active_hours: 10
  quiet_hours: ["23:30-07:00"]
on_failure:
  retry: 2
  then: Restart
  then_notify: true
  skip: true
```

- [ ] **Step 5: 追加 gitignore**

在 `.gitignore` 末尾追加：

```
# Agent runtime state (plans, journal, issues, workspace)
data/agent/
```

- [ ] **Step 6: 运行测试确认通过**

Run: `python -m pytest tests/agent/test_memory.py -v`
Expected: PASS，8 passed

- [ ] **Step 7: 提交**

```bash
git add module/server/agent/memory.py config/agent/daily_plan.example.yaml .gitignore tests/agent/test_memory.py
git commit -m "feat(agent): daily memory, issues and approval store"
```

---

### Task 4: 父进程侧桥接（帧缓存与命令投递）

**Files:**
- Create: `module/server/agent/bridge.py`
- Test: `tests/agent/test_bridge.py`

**Interfaces:**
- Consumes: 无（只依赖 `multiprocessing.Queue` 风格的 `put_nowait`/`get_nowait`）
- Produces:
  - `bridge.ALLOWED_COMMANDS: frozenset[str]`
  - `bridge.FrameStore`：`update(payload: bytes, ts: float)`、`latest() -> tuple[bytes|None, float|None]`、`age() -> float|None`
  - `bridge.FrameCollector(manager, store, interval: float = 0.2)`：`start()`、`stop()`、`poll_once()`
  - `bridge.Commander(manager)`：`send(name: str, command: str) -> None`（未知命令抛 `ValueError`，未知实例抛 `KeyError`）

- [ ] **Step 1: 写失败测试**

创建 `tests/agent/test_bridge.py`：

```python
import queue
import time

import pytest

from module.server.agent.bridge import ALLOWED_COMMANDS, Commander, FrameCollector, FrameStore


class FakeProcess:
    def __init__(self):
        self.frame_queue = queue.Queue(maxsize=1)
        self.cmd_queue = queue.Queue()


class FakeManager:
    def __init__(self, names=("oas1",)):
        self.script_process = {name: FakeProcess() for name in names}


def test_frame_store_returns_latest_and_age():
    store = FrameStore()
    assert store.latest() == (None, None)

    store.update(b"jpg-1", ts=100.0)
    store.update(b"jpg-2", ts=101.0)
    payload, ts = store.latest()
    assert payload == b"jpg-2"
    assert ts == 101.0
    assert store.age(now=106.0) == 5.0


def test_collector_drains_queue_and_keeps_newest():
    manager = FakeManager()
    proc = manager.script_process["oas1"]
    proc.frame_queue.put((b"one", 10.0))
    store = FrameStore()
    collector = FrameCollector(manager, store)

    collector.poll_once()
    assert store.latest() == (b"one", 10.0)

    proc.frame_queue.put((b"two", 11.0))
    collector.poll_once()
    assert store.latest() == (b"two", 11.0)


def test_collector_ignores_empty_and_missing_queue():
    manager = FakeManager()
    del manager.script_process["oas1"].frame_queue
    store = FrameStore()
    FrameCollector(manager, store).poll_once()
    assert store.latest() == (None, None)


def test_commander_pushes_known_command():
    manager = FakeManager()
    Commander(manager).send("oas1", "goto_main")
    assert manager.script_process["oas1"].cmd_queue.get_nowait() == "goto_main"


def test_commander_rejects_unknown_command():
    with pytest.raises(ValueError):
        Commander(FakeManager()).send("oas1", "rm -rf")


def test_commander_rejects_unknown_instance():
    with pytest.raises(KeyError):
        Commander(FakeManager()).send("nope", "goto_main")


def test_allowed_commands_cover_spec():
    assert ALLOWED_COMMANDS == frozenset(
        {"goto_main", "restart_game", "stop_game", "emulator_start", "emulator_stop"}
    )


def test_collector_start_stop_is_idempotent():
    collector = FrameCollector(FakeManager(), FrameStore(), interval=0.01)
    collector.start()
    collector.start()
    collector.stop()
    collector.stop()
    time.sleep(0.02)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/agent/test_bridge.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'module.server.agent.bridge'`

- [ ] **Step 3: 写实现**

创建 `module/server/agent/bridge.py`：

```python
# This Python file uses the following encoding: utf-8
"""父进程侧：收子进程的帧、给子进程发命令。"""
from __future__ import annotations

import threading
import time
from typing import Any

ALLOWED_COMMANDS: frozenset[str] = frozenset(
    {"goto_main", "restart_game", "stop_game", "emulator_start", "emulator_stop"}
)


class FrameStore:
    """按实例保存最新一帧。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frames: dict[str, tuple[bytes, float]] = {}

    def update(self, name: str, payload: bytes, ts: float) -> None:
        with self._lock:
            self._frames[name] = (payload, ts)

    def latest(self, name: str) -> tuple[bytes | None, float | None]:
        with self._lock:
            frame = self._frames.get(name)
            return (None, None) if frame is None else frame

    def age(self, name: str, now: float | None = None) -> float | None:
        with self._lock:
            frame = self._frames.get(name)
        if frame is None:
            return None
        current = time.time() if now is None else now
        return max(0.0, current - frame[1])


class FrameCollector:
    """后台线程：把每个实例的 frame_queue 抽干，写进 FrameStore。"""

    def __init__(self, manager: Any, store: FrameStore, interval: float = 0.2) -> None:
        self.manager = manager
        self.store = store
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_once(self) -> None:
        processes = getattr(self.manager, "script_process", {}) or {}
        for name, process in processes.items():
            frame_queue = getattr(process, "frame_queue", None)
            if frame_queue is None:
                continue
            while True:
                try:
                    payload, ts = frame_queue.get_nowait()
                except Exception:
                    break
                if payload:
                    self.store.update(name, payload, ts)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="AgentFrameCollector", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                pass
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None


class Commander:
    """把设备命令写进对应实例的 cmd_queue。"""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    def send(self, name: str, command: str) -> None:
        if command not in ALLOWED_COMMANDS:
            raise ValueError(f"unknown command: {command}")
        processes = getattr(self.manager, "script_process", {}) or {}
        if name not in processes:
            raise KeyError(name)
        queue = getattr(processes[name], "cmd_queue", None)
        if queue is None:
            raise KeyError(f"{name} has no command queue")
        queue.put(command)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/agent/test_bridge.py -v`
Expected: PASS，8 passed

- [ ] **Step 5: 提交**

```bash
git add module/server/agent/bridge.py tests/agent/test_bridge.py
git commit -m "feat(agent): parent-side frame store and command bridge"
```

---

### Task 5: 服务层只读路径

**Files:**
- Create: `tests/agent/fakes.py`
- Create: `module/server/agent/service.py`
- Test: `tests/agent/test_service_read.py`

**Interfaces:**
- Consumes: Task 1 `models`/`policy`、Task 2 `LogTailer`、Task 3 `AgentMemory`、Task 4 `FrameStore`/`Commander`
- Produces:
  - `service.UnknownInstance(KeyError)`
  - `service.AgentService(manager, *, memory=None, tailer=None, frame_store=None, commander=None, config_factory=None, now=None)`
  - 只读方法：`briefing() -> dict`、`list_instances() -> list[dict]`、`snapshot(name) -> dict`、
    `list_tasks(name) -> list[dict]`、`task_args(name, task) -> dict`、
    `log(name, cursor=None, limit=200) -> dict`、`screenshot(name) -> dict`
  - 内部：`_process(name)`、`_config(name)`、`_state_name(process)`

- [ ] **Step 1: 写测试替身**

创建 `tests/agent/fakes.py`：

```python
import queue

from module.config.utils import convert_to_underscore


def default_tasks():
    """最小任务表：一个普通任务 + 一个受保护任务 + Restart + script 配置。"""
    scheduler = {
        "enable": True,
        "next_run": "2023-01-01 00:00:00",
        "priority": 5,
        "success_interval": "01 00:00:00",
        "failure_interval": "01 00:00:00",
    }
    return {
        "running_task": "",
        "script": {"optimization": {"schedule_rule": "Filter", "queue_mode": False}},
        "restart": {"scheduler": dict(scheduler)},
        "exploration": {"scheduler": dict(scheduler, enable=False)},
        "mystery_shop": {"scheduler": dict(scheduler, enable=False)},
    }


class FakeConfig:
    def __init__(self, name="oas1", tasks=None):
        self.name = name
        self.data = tasks if tasks is not None else default_tasks()
        self.model = _FakeModel(self)

    def script_task(self, task):
        task = convert_to_underscore(task)
        node = self.data.get(task, {})
        return {group: [] for group in node}

    def script_set_arg(self, task, group, argument, value):
        task = convert_to_underscore(task)
        group = convert_to_underscore(group)
        node = self.data.get(task, {}).get(group)
        if node is None:
            return False
        node[argument] = value
        return True

    def task_call(self, task):
        task = convert_to_underscore(task)
        node = self.data.get(task, {}).get("scheduler")
        if node is None:
            return False
        node["next_run"] = "2026-10-23 18:15:00"
        return True

    def update_scheduler(self):
        self._pending = [
            {"name": key, "next_run": value["scheduler"]["next_run"]}
            for key, value in self.data.items()
            if isinstance(value, dict) and "scheduler" in value
            and value["scheduler"].get("enable")
        ]

    def get_schedule_data(self):
        return {"running": {}, "pending": getattr(self, "_pending", []), "waiting": []}


class _FakeModel:
    def __init__(self, config):
        self._config = config

    def dict(self):
        return self._config.data


class FakeProcess:
    def __init__(self, name="oas1", state="RUNNING"):
        self.name = name
        self.state = state
        self.alive = state == "RUNNING"
        self.cmd_queue = queue.Queue()
        self.frame_queue = queue.Queue(maxsize=1)
        self.started = 0
        self.stopped = 0

    def is_alive(self):
        return self.alive

    async def start(self):
        self.state = "RUNNING"
        self.alive = True
        self.started += 1

    async def stop(self):
        self.state = "INACTIVE"
        self.alive = False
        self.stopped += 1


class FakeManager:
    def __init__(self, names=("oas1",), configs=None):
        self.script_process = {name: FakeProcess(name) for name in names}
        self._configs = configs or {name: FakeConfig(name) for name in names}

    def config_cache(self, name):
        if name not in self._configs:
            raise FileNotFoundError(name)
        return self._configs[name]
```

- [ ] **Step 2: 写失败测试**

创建 `tests/agent/test_service_read.py`：

```python
import queue

from module.server.agent.bridge import FrameStore
from module.server.agent.logtail import LogTailer
from module.server.agent.memory import AgentMemory
from module.server.agent.service import AgentService, UnknownInstance
from tests.agent.fakes import FakeManager


def _service(tmp_path, manager=None):
    manager = manager or FakeManager()
    return AgentService(
        manager,
        memory=AgentMemory(root=tmp_path),
        tailer=LogTailer(root=tmp_path),
        frame_store=FrameStore(),
    )


def test_unknown_instance_raises(tmp_path):
    service = _service(tmp_path)
    try:
        service.snapshot("nope")
    except UnknownInstance as error:
        assert "nope" in str(error)
    else:
        raise AssertionError("expected UnknownInstance")


def test_list_instances_reports_state_and_running_task(tmp_path):
    manager = FakeManager()
    manager.config_cache("oas1").data["running_task"] = "Orochi"
    payload = _service(tmp_path, manager).list_instances()
    assert payload[0]["name"] == "oas1"
    assert payload[0]["state"] == "RUNNING"
    assert payload[0]["pid_alive"] is True
    assert payload[0]["running_task"] == "Orochi"


def test_list_instances_frame_age_is_none_without_frames(tmp_path):
    payload = _service(tmp_path).list_instances()
    assert payload[0]["frame_age_s"] is None


def test_snapshot_includes_schedule_and_hold(tmp_path):
    payload = _service(tmp_path).snapshot("oas1")
    assert payload["name"] == "oas1"
    assert set(payload["schedule"]) == {"running", "pending", "waiting"}
    assert payload["hold"]["active"] is False


def test_list_tasks_only_returns_scheduler_tasks(tmp_path):
    tasks = _service(tmp_path).list_tasks("oas1")
    names = {item["task"] for item in tasks}
    assert names == {"restart", "exploration", "mystery_shop"}
    exploration = next(item for item in tasks if item["task"] == "exploration")
    assert exploration["enable"] is False
    assert exploration["priority"] == 5


def test_task_args_returns_schema_groups(tmp_path):
    args = _service(tmp_path).task_args("oas1", "Exploration")
    assert "scheduler" in args


def test_log_tail_from_empty_directory(tmp_path):
    payload = _service(tmp_path).log("oas1")
    assert payload["lines"] == []
    assert payload["cursor"] == 0


def test_screenshot_reports_no_frame_explicitly(tmp_path):
    payload = _service(tmp_path).screenshot("oas1")
    assert payload["available"] is False
    assert payload["image"] is None


def test_screenshot_returns_frame_and_age(tmp_path):
    manager = FakeManager()
    service = _service(tmp_path, manager)
    service.frame_store.update(b"jpeg-bytes", ts=1000.0)
    payload = service.screenshot("oas1")
    assert payload["available"] is True
    assert payload["image"] == b"jpeg-bytes"


def test_briefing_contains_plan_state_and_hold(tmp_path):
    payload = _service(tmp_path).briefing()
    assert payload["instances"][0]["name"] == "oas1"
    assert payload["plan"]["timezone"] == "Asia/Shanghai"
    assert "done" in payload["state"]
    assert payload["hold"]["active"] is False
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/agent/test_service_read.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'module.server.agent.service'`

- [ ] **Step 4: 写只读实现**

创建 `module/server/agent/service.py`：

```python
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
        payload, ts = self.frame_store.latest(name)
        age = self.frame_store.age(name)
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
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/agent/test_service_read.py -v`
Expected: PASS，10 passed

- [ ] **Step 6: 提交**

```bash
git add module/server/agent/service.py tests/agent/fakes.py tests/agent/test_service_read.py
git commit -m "feat(agent): read-only capability service"
```

---

### Task 6: 服务层写路径与闸门

**Files:**
- Modify: `module/server/agent/service.py`
- Test: `tests/agent/test_service_write.py`

**Interfaces:**
- Consumes: Task 5 的 `AgentService`
- Produces:
  - 同步写：`set_task_enabled(name, task, enabled) -> dict`、`set_task_arg(name, task, group, argument, value) -> dict`、
    `set_next_run(name, task, at) -> dict`、`set_schedule_rule(name, rule) -> dict`、
    `send_command(name, command) -> dict`、`hold(minutes=120, reason="") -> dict`、`release_hold() -> dict`、
    `update_daily_state(patch) -> dict`、`journal(text) -> dict`、`file_issue(...) -> dict`、`list_issues(status=None) -> list[dict]`
  - 异步写：`async set_running(name, running) -> dict`、`async run_now(name, task, position="front") -> dict`、`async emergency_stop(minutes=120) -> dict`
  - 所有写返回 `models.WriteResult.to_dict()` 形状

- [ ] **Step 1: 写失败测试**

创建 `tests/agent/test_service_write.py`：

```python
import asyncio
from datetime import date, datetime

import pytest

from module.server.agent.logtail import LogTailer
from module.server.agent.memory import AgentMemory
from module.server.agent.policy import HoldActive, NeedsApproval
from module.server.agent.service import AgentService
from tests.agent.fakes import FakeManager

FIXED_NOW = datetime(2026, 10, 23, 18, 15, 0)


def _service(tmp_path, manager=None):
    manager = manager or FakeManager()
    return AgentService(
        manager,
        memory=AgentMemory(root=tmp_path),
        tailer=LogTailer(root=tmp_path),
        now=lambda: FIXED_NOW,
    )


def test_set_task_enabled_writes_and_verifies(tmp_path):
    manager = FakeManager()
    result = _service(tmp_path, manager).set_task_enabled("oas1", "Exploration", True)
    assert result["ok"] is True
    assert result["effective"] == "next_task_boundary"
    assert manager.config_cache("oas1").data["exploration"]["scheduler"]["enable"] is True


def test_set_task_enabled_rejects_unknown_task(tmp_path):
    with pytest.raises(ValueError):
        _service(tmp_path).set_task_enabled("oas1", "NotATask", True)


def test_set_next_run_formats_timestamp(tmp_path):
    manager = FakeManager()
    result = _service(tmp_path, manager).set_next_run("oas1", "Exploration", "2026-10-24 07:00:00")
    assert result["detail"]["next_run"] == "2026-10-24 07:00:00"
    assert manager.config_cache("oas1").data["exploration"]["scheduler"]["next_run"] == "2026-10-24 07:00:00"


def test_set_next_run_rejects_bad_format(tmp_path):
    with pytest.raises(ValueError):
        _service(tmp_path).set_next_run("oas1", "Exploration", "tomorrow")


def test_run_now_front_uses_current_time_and_starts_instance(tmp_path):
    manager = FakeManager()
    manager.script_process["oas1"].state = "INACTIVE"
    manager.script_process["oas1"].alive = False
    service = _service(tmp_path, manager)

    result = asyncio.run(service.run_now("oas1", "Exploration", position="front"))
    assert result["ok"] is True
    assert manager.config_cache("oas1").data["exploration"]["scheduler"]["next_run"] == "2026-10-23 18:15:00"
    assert manager.script_process["oas1"].started == 1


def test_run_now_end_adds_offset(tmp_path):
    manager = FakeManager()
    service = _service(tmp_path, manager)
    asyncio.run(service.run_now("oas1", "Exploration", position="end"))
    assert manager.config_cache("oas1").data["exploration"]["scheduler"]["next_run"] == "2026-10-23 18:20:00"


def test_run_now_rejects_bad_position(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(_service(tmp_path).run_now("oas1", "Exploration", position="middle"))


def test_send_command_puts_on_queue(tmp_path):
    manager = FakeManager()
    result = _service(tmp_path, manager).send_command("oas1", "goto_main")
    assert result["ok"] is True
    assert manager.script_process["oas1"].cmd_queue.get_nowait() == "goto_main"


def test_send_command_rejects_unknown(tmp_path):
    with pytest.raises(ValueError):
        _service(tmp_path).send_command("oas1", "shutdown")


def test_protected_task_write_needs_approval(tmp_path):
    with pytest.raises(NeedsApproval) as info:
        _service(tmp_path).set_task_enabled("oas1", "MysteryShop", True)
    assert info.value.detail["task"] == "mystery_shop"
    assert info.value.detail["approval_token"]


def test_approval_token_unlocks_one_protected_write(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(NeedsApproval) as info:
        service.set_task_enabled("oas1", "MysteryShop", True)
    token = info.value.detail["approval_token"]

    assert service.memory.grant_approval(token) is True

    result = service.set_task_enabled("oas1", "MysteryShop", True, approval_token=token)
    assert result["ok"] is True
    with pytest.raises(NeedsApproval):
        service.set_task_enabled("oas1", "MysteryShop", False)


def test_hold_blocks_all_writes(tmp_path):
    service = _service(tmp_path)
    service.hold(minutes=60, reason="playing")
    with pytest.raises(HoldActive):
        service.set_task_enabled("oas1", "Exploration", True)
    assert service.release_hold()["hold"]["active"] is False
    assert service.set_task_enabled("oas1", "Exploration", True)["ok"] is True


def test_schedule_rule_is_validated(tmp_path):
    manager = FakeManager()
    service = _service(tmp_path, manager)
    assert service.set_schedule_rule("oas1", "FIFO")["ok"] is True
    assert manager.config_cache("oas1").data["script"]["optimization"]["schedule_rule"] == "FIFO"
    with pytest.raises(ValueError):
        service.set_schedule_rule("oas1", "Random")


def test_emergency_stop_stops_every_instance_and_holds(tmp_path):
    manager = FakeManager(names=("oas1", "oas2"))
    service = _service(tmp_path, manager)
    result = asyncio.run(service.emergency_stop())
    assert result["stopped"] == ["oas1", "oas2"]
    assert manager.script_process["oas1"].stopped == 1
    assert service.briefing()["hold"]["active"] is True


def test_daily_state_and_journal_are_persisted(tmp_path):
    service = _service(tmp_path)
    state = service.update_daily_state({"done": ["Restart"]})
    assert state["done"] == ["Restart"]
    payload = service.journal("插队 Exploration")
    assert payload["path"].endswith(".md")


def test_file_issue_captures_snapshot_and_log(tmp_path):
    service = _service(tmp_path)
    (tmp_path / f"{date.today().isoformat()}_oas1.txt").write_text("boom\n", encoding="utf-8")

    issue = service.file_issue(task="Orochi", symptom="连续失败")
    assert issue["task"] == "Orochi"
    assert "boom" in issue["log_excerpt"]
    assert issue["snapshot"]["name"] == "oas1"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/agent/test_service_write.py -v`
Expected: FAIL，`AttributeError: 'AgentService' object has no attribute 'set_task_enabled'`

- [ ] **Step 3: 在 `service.py` 补写路径**

在 `module/server/agent/service.py` 的 import 段补：

```python
import uuid

from module.server.agent.policy import ActionClass, HoldActive, NeedsApproval, classify_write
```

并把文件顶部的时间 import 补上 `timedelta`：

```python
from datetime import datetime, timedelta
```

在 `AgentService` 类末尾追加（放在 `briefing` 之后）：

```python
    # --------------------------------------------------------- guard rails
    def _guard(self, kind: str, task: str | None = None, approval_token: str | None = None) -> None:
        hold = self._hold.state()
        if hold.active and kind not in {"hold", "release_hold", "estop"}:
            raise HoldActive("hold is active; writes are disabled", hold.to_dict())

        protected = self.memory.plan().get("protected_tasks") or ()
        if classify_write(kind, task, protected) is not ActionClass.IRREVERSIBLE:
            return

        if approval_token and self.memory.consume_approval(approval_token):
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
        if not verify_fn(fresh):
            retry_config = self._config(name)
            apply_fn(retry_config)
            fresh = self._config(name)
            if not verify_fn(fresh):
                self.memory.append_journal(f"写入校验失败 kind={kind} task={task}")
                raise ValueError(f"verify failed: kind={kind} task={task}")

        payload = dict(detail or {})
        payload.setdefault("instance", name)
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
            node = config.model.dict()[key].get(group) or {}
            return node.get(argument) == value

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
        self._guard("instance")
        process = self._process(name)
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
        self._guard("estop")
        processes = getattr(self.manager, "script_process", {}) or {}
        stopped = []
        for name, process in processes.items():
            try:
                await process.stop()
                stopped.append(name)
            except Exception:
                continue
        hold = self._hold.activate(minutes=minutes, reason="emergency_stop")
        self.memory.append_journal(f"急停：停止 {', '.join(stopped) or '无实例'}，让位 {minutes} 分钟")
        return {"ok": True, "stopped": stopped, "hold": hold.to_dict()}

    # ------------------------------------------------------- hold & memory
    def hold(self, minutes: int = 120, reason: str = "") -> dict:
        state = self._hold.activate(minutes=minutes, reason=reason)
        self.memory.append_journal(f"让位 {minutes} 分钟：{reason or '未说明'}")
        return {"ok": True, "hold": state.to_dict()}

    def release_hold(self) -> dict:
        state = self._hold.release()
        self.memory.append_journal("结束让位")
        return {"ok": True, "hold": state.to_dict()}

    def update_daily_state(self, patch: dict) -> dict:
        return self.memory.update_state(patch)

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
            payload, _ = self.frame_store.latest()
            frame = payload
        return self.memory.file_issue(
            task=task,
            symptom=symptom,
            suggestion=suggestion,
            log_excerpt=log_excerpt,
            snapshot=snapshot,
            frame=frame,
        )
```

同时把文件顶部 import 补上 `timedelta`：

```python
from datetime import datetime, timedelta
```

`_guard` 里的 `ActionClass` 是 `str` 子类，所以返回的 JSON 里也是可读字符串。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/agent/test_service_write.py -v`
Expected: PASS，16 passed

- [ ] **Step 5: 跑全部已有测试确认没破坏**

Run: `python -m pytest tests/agent -v`
Expected: PASS，全部通过

- [ ] **Step 6: 提交**

```bash
git add module/server/agent/service.py tests/agent/test_service_write.py
git commit -m "feat(agent): write path with hold and approval gates"
```

---

### Task 7: REST 路由与回环中间件

**Files:**
- Create: `module/server/agent_router.py`
- Create: `module/server/agent/app_factory.py`
- Modify: `module/server/app.py`（挂载）
- Test: `tests/agent/test_router.py`
- Test: `tests/agent/test_loopback.py`

**Interfaces:**
- Consumes: Task 5/6 的 `AgentService`
- Produces:
  - `agent_router.create_agent_router(service) -> APIRouter`（前缀 `/api/agent`）
  - `app_factory.LOOPBACK_HOSTS: set[str]`
  - `app_factory.LoopbackOnlyMiddleware(app)`（纯 ASGI）
  - `app_factory.build_service(manager) -> AgentService`
  - `app_factory.install_agent(app, service=None, manager=None, mount_mcp=True) -> AgentService`

- [ ] **Step 1: 写失败测试（路由）**

创建 `tests/agent/test_router.py`：

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient

from module.server.agent.logtail import LogTailer
from module.server.agent.memory import AgentMemory
from module.server.agent.service import AgentService
from module.server.agent_router import create_agent_router
from tests.agent.fakes import FakeManager


def _client(tmp_path, manager=None):
    manager = manager or FakeManager()
    service = AgentService(
        manager, memory=AgentMemory(root=tmp_path), tailer=LogTailer(root=tmp_path)
    )
    app = FastAPI()
    app.include_router(create_agent_router(service))
    return TestClient(app), manager


def test_list_instances(tmp_path):
    client, _ = _client(tmp_path)
    response = client.get("/api/agent/instances")
    assert response.status_code == 200
    assert response.json()[0]["name"] == "oas1"


def test_unknown_instance_is_404(tmp_path):
    client, _ = _client(tmp_path)
    response = client.get("/api/agent/nope/snapshot")
    assert response.status_code == 404


def test_snapshot(tmp_path):
    client, _ = _client(tmp_path)
    response = client.get("/api/agent/oas1/snapshot")
    assert response.status_code == 200
    assert response.json()["name"] == "oas1"


def test_set_enabled_writes(tmp_path):
    client, manager = _client(tmp_path)
    response = client.post("/api/agent/oas1/tasks/Exploration/enable", json={"enabled": True})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert manager.config_cache("oas1").data["exploration"]["scheduler"]["enable"] is True


def test_protected_write_returns_403_with_token(tmp_path):
    client, _ = _client(tmp_path)
    response = client.post("/api/agent/oas1/tasks/MysteryShop/enable", json={"enabled": True})
    assert response.status_code == 403
    assert response.json()["detail"]["approval_token"]


def test_hold_blocks_write_with_423(tmp_path):
    client, _ = _client(tmp_path)
    client.post("/api/agent/hold", json={"minutes": 30, "reason": "playing"})
    response = client.post("/api/agent/oas1/tasks/Exploration/enable", json={"enabled": True})
    assert response.status_code == 423
    client.delete("/api/agent/hold")
    assert client.post("/api/agent/oas1/tasks/Exploration/enable", json={"enabled": True}).status_code == 200


def test_bad_payload_is_422(tmp_path):
    client, _ = _client(tmp_path)
    assert client.post("/api/agent/oas1/tasks/Exploration/enable", json={}).status_code == 422


def test_next_run_validation_is_400(tmp_path):
    client, _ = _client(tmp_path)
    response = client.post("/api/agent/oas1/tasks/Exploration/next_run", json={"at": "tomorrow"})
    assert response.status_code == 400


def test_run_now_and_command(tmp_path):
    client, manager = _client(tmp_path)
    assert client.post("/api/agent/oas1/tasks/Exploration/run_now", json={"position": "front"}).status_code == 200
    assert client.post("/api/agent/oas1/command", json={"command": "goto_main"}).status_code == 200
    assert manager.script_process["oas1"].cmd_queue.get_nowait() == "goto_main"


def test_start_stop_instance(tmp_path):
    client, manager = _client(tmp_path)
    assert client.post("/api/agent/oas1/running", json={"running": False}).status_code == 200
    assert manager.script_process["oas1"].stopped == 1


def test_emergency_stop(tmp_path):
    client, manager = _client(tmp_path)
    response = client.post("/api/agent/emergency_stop", json={"minutes": 30})
    assert response.status_code == 200
    assert response.json()["stopped"] == ["oas1"]


def test_log_and_briefing_and_issues(tmp_path):
    client, _ = _client(tmp_path)
    assert client.get("/api/agent/oas1/log").status_code == 200
    assert client.get("/api/agent/briefing").status_code == 200
    assert client.post("/api/agent/issues", json={"task": "Orochi", "symptom": "x"}).status_code == 200
    assert client.get("/api/agent/issues").status_code == 200
```

- [ ] **Step 2: 写失败测试（回环）**

创建 `tests/agent/test_loopback.py`：

```python
import asyncio

from module.server.agent.app_factory import LoopbackOnlyMiddleware


def _run(scope, inner_status=200):
    sent = []

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": inner_status, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    middleware = LoopbackOnlyMiddleware(inner)
    asyncio.run(middleware(scope, receive, send))
    return sent[0]["status"]


def _scope(path, host):
    return {"type": "http", "method": "GET", "path": path, "headers": [], "client": (host, 1234)}


def test_loopback_is_allowed():
    assert _run(_scope("/api/agent/instances", "127.0.0.1")) == 200


def test_ipv6_loopback_is_allowed():
    assert _run(_scope("/api/agent/instances", "::1")) == 200


def test_lan_client_is_rejected():
    assert _run(_scope("/api/agent/instances", "192.168.1.20")) == 403


def test_mcp_path_is_protected():
    assert _run(_scope("/mcp", "10.0.0.5")) == 403


def test_normal_oas_path_is_not_protected():
    assert _run(_scope("/config_all", "10.0.0.5")) == 200
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/agent/test_router.py tests/agent/test_loopback.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'module.server.agent_router'`

- [ ] **Step 4: 写路由实现**

创建 `module/server/agent_router.py`：

```python
# This Python file uses the following encoding: utf-8
"""REST 入口 /api/agent/*，与 MCP 工具共用 AgentService。"""
from __future__ import annotations

import inspect
from functools import wraps
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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
    return HTTPException(status_code=500, detail=str(error))


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
```

REST 侧把截图拆成两个端点：`/screenshot` 返回 JSON 元信息（`available`、`age_s`），`/screenshot.jpg` 返回原始 JPEG。spec 里写的是单端点直接回 `image/jpeg`；拆开后 curl 调试和测试都更省事，MCP 侧不受影响。

- [ ] **Step 5: 写 app_factory**

创建 `module/server/agent/app_factory.py`：

```python
# This Python file uses the following encoding: utf-8
"""把 agent 能力层装进 FastAPI：路由、回环中间件、MCP 挂载。"""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from module.server.agent.service import AgentService
from module.server.agent_router import create_agent_router

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
PROTECTED_PREFIXES = ("/api/agent", "/mcp")


class LoopbackOnlyMiddleware:
    """纯 ASGI 中间件：非回环地址访问受保护前缀一律 403。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and str(scope.get("path", "")).startswith(PROTECTED_PREFIXES):
            client = scope.get("client")
            host = client[0] if client else None
            if host not in LOOPBACK_HOSTS:
                response = JSONResponse(
                    {"error": "forbidden", "detail": "agent endpoints are loopback-only"},
                    status_code=403,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_service(manager: Any) -> AgentService:
    return AgentService(manager)


def install_agent(
    app: FastAPI,
    service: AgentService | None = None,
    manager: Any = None,
    mount_mcp: bool = True,
) -> AgentService:
    if service is None:
        if manager is None:
            raise ValueError("install_agent needs either service or manager")
        service = build_service(manager)

    app.add_middleware(LoopbackOnlyMiddleware)
    app.include_router(create_agent_router(service))
    app.state.agent_service = service

    if mount_mcp:
        from module.server.mcp_server import create_mcp

        mcp = create_mcp(service)
        app.state.agent_mcp = mcp
        app.mount("/mcp", mcp.streamable_http_app())
    return service
```

- [ ] **Step 6: 在 `module/server/app.py` 挂载**

在 `module/server/app.py` 顶部 import 区追加：

```python
from module.server.agent.app_factory import install_agent
```

在 `app.include_router(tool_app)` 之后追加：

```python
agent_service = install_agent(app, manager=mm, mount_mcp=False)
```

并在 `on_startup` 里启动帧收集线程：

```python
from module.server.agent.bridge import FrameCollector

_frame_collector: FrameCollector = None


async def on_startup():
    global _frame_collector
    logger.info('OAS web service startup done')
    _frame_collector = FrameCollector(mm, agent_service.frame_store)
    _frame_collector.start()
    if app.state.script_instances:
        await mm.restart_processes(app.state.script_instances)
```

`mount_mcp=False`：MCP 挂载在 Task 8 完成后改为 `True`，因为 `mcp_server` 模块那时才存在。

- [ ] **Step 7: 运行测试确认通过**

Run: `python -m pytest tests/agent/test_router.py tests/agent/test_loopback.py -v`
Expected: PASS，17 passed

- [ ] **Step 8: 提交**

```bash
git add module/server/agent_router.py module/server/agent/app_factory.py module/server/app.py tests/agent/test_router.py tests/agent/test_loopback.py
git commit -m "feat(agent): REST router, loopback guard and app wiring"
```

---

### Task 8: MCP 服务与工具

**Files:**
- Create: `module/server/mcp_server.py`
- Modify: `module/server/app.py`（`mount_mcp=True` 与 lifespan 里启动 session manager）
- Modify: `requirements-in.txt`（加 `mcp==1.27.1`）
- Test: `tests/agent/test_mcp_tools.py`

**Interfaces:**
- Consumes: Task 5/6 的 `AgentService`
- Produces: `mcp_server.create_mcp(service) -> FastMCP`，工具名固定为
  `get_briefing`、`list_instances`、`get_snapshot`、`list_tasks`、`get_task_args`、`get_log`、`screenshot`、
  `set_instance_running`、`send_device_command`、`set_task_enabled`、`set_task_args`、`set_task_next_run`、
  `run_task_now`、`set_schedule_rule`、`hold_device`、`release_device`、`emergency_stop`、
  `update_daily_state`、`log_decision`、`file_issue`、`list_issues`

- [ ] **Step 1: 写失败测试**

创建 `tests/agent/test_mcp_tools.py`：

```python
import asyncio
import json

from module.server.agent.logtail import LogTailer
from module.server.agent.memory import AgentMemory
from module.server.agent.service import AgentService
from module.server.mcp_server import TOOL_NAMES, create_mcp
from tests.agent.fakes import FakeManager


def _mcp(tmp_path, manager=None):
    manager = manager or FakeManager()
    service = AgentService(
        manager, memory=AgentMemory(root=tmp_path), tailer=LogTailer(root=tmp_path)
    )
    return create_mcp(service), manager


def test_tool_names_are_complete():
    assert len(TOOL_NAMES) == 21
    assert "get_briefing" in TOOL_NAMES
    assert "file_issue" in TOOL_NAMES


def test_list_tools_exposes_expected_names(tmp_path):
    mcp, _ = _mcp(tmp_path)

    async def collect():
        return await mcp.list_tools()

    tools = asyncio.run(collect())
    assert {tool.name for tool in tools} == set(TOOL_NAMES)


def test_call_tool_list_instances_returns_json_text(tmp_path):
    mcp, _ = _mcp(tmp_path)

    async def call():
        result = await mcp.call_tool("list_instances", {})
        return result

    content, structured = asyncio.run(call())
    text = content[0].text
    assert json.loads(text)[0]["name"] == "oas1"


def test_call_tool_set_task_enabled(tmp_path):
    mcp, manager = _mcp(tmp_path)

    async def call():
        return await mcp.call_tool("set_task_enabled", {"instance": "oas1", "task": "Exploration", "enabled": True})

    content, _ = asyncio.run(call())
    payload = json.loads(content[0].text)
    assert payload["ok"] is True
    assert manager.config_cache("oas1").data["exploration"]["scheduler"]["enable"] is True


def test_call_tool_screenshot_without_frame_is_diagnostic(tmp_path):
    mcp, _ = _mcp(tmp_path)

    async def call():
        return await mcp.call_tool("screenshot", {"instance": "oas1"})

    content, _ = asyncio.run(call())
    assert "no frame" in content[0].text.lower()


def test_screenshot_returns_image_content(tmp_path):
    import time

    manager = FakeManager()
    service = AgentService(
        manager, memory=AgentMemory(root=tmp_path), tailer=LogTailer(root=tmp_path)
    )
    service.frame_store.update(b"\xff\xd8jpeg", ts=time.time())
    mcp = create_mcp(service)

    async def call():
        return await mcp.call_tool("screenshot", {"instance": "oas1"})

    blocks = asyncio.run(call())
    assert isinstance(blocks, list)
    assert blocks[0].type == "image"


def test_tool_error_surfaces_not_swallowed(tmp_path):
    mcp, _ = _mcp(tmp_path)

    async def call():
        return await mcp.call_tool("get_snapshot", {"instance": "nope"})

    try:
        asyncio.run(call())
    except Exception as error:  # ToolError from the SDK
        assert "nope" in str(error)
    else:
        raise AssertionError("expected a tool error")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/agent/test_mcp_tools.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'module.server.mcp_server'`

- [ ] **Step 3: 写 MCP 服务**

创建 `module/server/mcp_server.py`：

```python
# This Python file uses the following encoding: utf-8
"""MCP 入口：把 AgentService 包成工具。"""
from __future__ import annotations

import base64
import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from module.server.agent.service import AgentService

INSTRUCTIONS = (
    "OAS 游戏自动化控制台。所有工具都是非阻塞的：写操作不会打断正在运行的任务，"
    "改动在下一个任务边界生效；要立刻停下某个实例，用 set_instance_running(false)。"
    "每日编排前先调用 get_briefing 拿到计划、当日状态和实例快照。"
    "任务失败两次就跳过并 file_issue，不要反复重试同一个任务。"
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
    "release_device",
    "emergency_stop",
    "update_daily_state",
    "log_decision",
    "file_issue",
    "list_issues",
)


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


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
    async def set_instance_running(instance: str, running: bool) -> str:
        """启动或停止实例子进程。停止是唯一能立刻中断当前任务的操作。"""
        return _dump(await service.set_running(instance, running))

    @mcp.tool()
    def send_device_command(instance: str, command: str) -> str:
        """下发设备命令，下一个任务边界生效。command: goto_main/restart_game/stop_game/emulator_start/emulator_stop。"""
        return _dump(service.send_command(instance, command))

    @mcp.tool()
    def set_task_enabled(instance: str, task: str, enabled: bool, approval_token: str | None = None) -> str:
        """开关某个任务。受保护任务（勾玉/付费相关）需要人工批准后带 approval_token 调用。"""
        return _dump(service.set_task_enabled(instance, task, enabled, approval_token=approval_token))

    @mcp.tool()
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
    def set_task_next_run(instance: str, task: str, at: str, approval_token: str | None = None) -> str:
        """设置下次运行时间，格式 'YYYY-MM-DD HH:MM:SS'。"""
        return _dump(service.set_next_run(instance, task, at, approval_token=approval_token))

    @mcp.tool()
    async def run_task_now(instance: str, task: str, position: str = "front") -> str:
        """立刻补跑某任务。position=front 插到队首，end 排到 5 分钟后。必要时自动启动实例。"""
        return _dump(await service.run_now(instance, task, position=position))

    @mcp.tool()
    def set_schedule_rule(instance: str, rule: str) -> str:
        """设置调度规则：Filter / FIFO / Priority。"""
        return _dump(service.set_schedule_rule(instance, rule))

    @mcp.tool()
    def hold_device(minutes: int = 120, reason: str = "") -> str:
        """让位：人要用设备时调用，期间所有写操作被拒绝，到点自动解除。"""
        return _dump(service.hold(minutes=minutes, reason=reason))

    @mcp.tool()
    def release_device() -> str:
        """结束让位。"""
        return _dump(service.release_hold())

    @mcp.tool()
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/agent/test_mcp_tools.py -v`
Expected: PASS，7 passed

已知的 SDK 行为（已实测）：返回 `str` 的工具，`call_tool` 返回 `(content, structured)` 二元组；返回 `Image` 的工具，`call_tool` 直接返回 `[ImageContent]` 列表。测试按这个差异分开断言。

- [ ] **Step 5: 挂载 MCP 并接线 lifespan**

把 `module/server/app.py` 中的 `install_agent(app, manager=mm, mount_mcp=False)` 改为：

```python
agent_service = install_agent(app, manager=mm, mount_mcp=True)
```

把 `lifespan` 改为：

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    mcp = getattr(app.state, "agent_mcp", None)
    if mcp is None:
        await on_startup()
        yield
        await on_shutdown()
        return
    async with mcp.session_manager.run():
        await on_startup()
        yield
        await on_shutdown()
```

- [ ] **Step 6: 固定依赖**

在 `requirements-in.txt` 的 `# Web` 段、`pydantic==2.10.0` 之后追加：

```
mcp==1.27.1
```

`requirements.txt` 是由 `pip-compile` 从 `requirements-in.txt` 生成的。改完 `requirements-in.txt` 后必须重新编译，否则按 `requirements.txt` 装的环境里没有 `mcp`：

```bash
pip-compile --annotation-style=line --output-file=requirements.txt requirements-in.txt
```

本机已安装 `mcp==1.27.1`，测试不依赖这一步；但交付前要重新生成并提交 `requirements.txt`。

- [ ] **Step 7: 冒烟验证挂载与路由共存**

Run:

```bash
python -c "from module.server import app as a; print([r.path for r in a.app.routes if getattr(r,'path','').startswith(('/api/agent','/mcp'))][:5])"
```

Expected: 输出里能看到 `/mcp` 与 `/api/agent/briefing`

- [ ] **Step 8: 提交**

```bash
git add module/server/mcp_server.py module/server/app.py requirements-in.txt tests/agent/test_mcp_tools.py
git commit -m "feat(agent): MCP server over streamable-http"
```

---

### Task 9: 子进程侧的命令消费与帧发布

**Files:**
- Modify: `module/server/script_process.py`
- Modify: `script.py`
- Test: `tests/agent/test_script_agent_hooks.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `ScriptProcess.cmd_queue`、`ScriptProcess.frame_queue`（`multiprocessing.Queue`）
  - `ScriptProcess.is_alive() -> bool`
  - `Script.__init__(config_name, cmd_queue=None, frame_queue=None)`
  - `Script._drain_agent_commands()`、`Script._handle_agent_command(command)`、`Script._publish_frame_once()`、`Script._start_frame_publisher()`

- [ ] **Step 1: 写失败测试**

创建 `tests/agent/test_script_agent_hooks.py`：

```python
import queue

import numpy as np
import pytest

from script import Script


class FakeDevice:
    def __init__(self, image=None):
        self.image = image
        self.calls = []

    def app_stop(self):
        self.calls.append("app_stop")

    def emulator_stop(self):
        self.calls.append("emulator_stop")


class FakeConfig:
    def __init__(self):
        self.calls = []

    def task_call(self, task):
        self.calls.append(task)
        return True


def _script(cmd_queue=None, frame_queue=None):
    script = Script("oas1", cmd_queue=cmd_queue, frame_queue=frame_queue)
    script.__dict__["config"] = FakeConfig()
    script.__dict__["device"] = FakeDevice()
    return script


def test_goto_main_runs_gotomaintask():
    ran = []
    script = _script()
    script.run = lambda command: ran.append(command) or True
    script._handle_agent_command("goto_main")
    assert ran == ["GotoMain"]


def test_restart_game_queues_restart_task():
    script = _script()
    script._handle_agent_command("restart_game")
    assert script.config.calls == ["Restart"]


def test_stop_and_emulator_stop_call_device():
    script = _script()
    script._handle_agent_command("stop_game")
    script._handle_agent_command("emulator_stop")
    assert script.device.calls == ["app_stop", "emulator_stop"]
    assert script._emulator_down is True


def test_unknown_command_is_ignored_not_raised():
    _script()._handle_agent_command("self_destruct")


def test_drain_consumes_all_queued_commands():
    cmd_queue = queue.Queue()
    cmd_queue.put("restart_game")
    cmd_queue.put("stop_game")
    script = _script(cmd_queue=cmd_queue)
    script._drain_agent_commands()
    assert script.device.calls == ["app_stop"]
    assert cmd_queue.empty()


def test_publish_frame_pushes_jpeg():
    image = np.zeros((48, 64, 3), dtype=np.uint8)
    frame_queue = queue.Queue(maxsize=1)
    script = _script(frame_queue=frame_queue)
    script.device.image = image

    script._publish_frame_once()
    payload, ts = frame_queue.get_nowait()
    assert payload[:2] == b"\xff\xd8"
    assert ts > 0


def test_publish_frame_skips_without_image():
    frame_queue = queue.Queue(maxsize=1)
    script = _script(frame_queue=frame_queue)
    del script.device.image
    script._publish_frame_once()
    assert frame_queue.empty()


def test_publish_frame_keeps_queue_at_one():
    image = np.zeros((16, 16, 3), dtype=np.uint8)
    frame_queue = queue.Queue(maxsize=1)
    script = _script(frame_queue=frame_queue)
    script.device.image = image
    script._publish_frame_once()
    script._publish_frame_once()
    assert frame_queue.qsize() == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/agent/test_script_agent_hooks.py -v`
Expected: FAIL，`TypeError: __init__() got an unexpected keyword argument 'cmd_queue'`

- [ ] **Step 3: 改 `script.py`**

把 `Script.__init__` 整个替换为：

```python
    def __init__(self, config_name: str = 'oas',
                 cmd_queue=None, frame_queue=None) -> None:
        logger.hr('Start', level=0)
        self.server = None
        self.state_queue: Queue = None
        self._emulator_down = False
        self.gui_update_task: Callable = None  # 回调函数, gui进程注册当每次config更新任务的时候更新gui的信息
        self.config_name = config_name
        self.cmd_queue = cmd_queue
        self.frame_queue = frame_queue
        self._in_agent_command = False
        # Skip first restart
        self.is_first_task = True
        # Failure count of tasks
        # Key: str, task name, value: int, failure count
        self.failure_record = {}
        # 运行loop的线程
        self.loop_thread: Thread = None
        # 跨进程排队管理器（仅在 queue_mode=True 时初始化）
        self.instance_guard: InstanceGuard = None
        self.anti_ban_guard: AntiBanGuard = AntiBanGuard()
```

在 `Script` 类里、`loop()` 之前新增四个方法：

```python
    # ------------------------------ agent bridge ------------------------------
    _FRAME_INTERVAL = 5.0
    _FRAME_MAX_EDGE = 1280
    _FRAME_QUALITY = 70

    def _handle_agent_command(self, command: str) -> None:
        """执行一条来自 agent 的命令，只允许白名单。"""
        try:
            if command == 'goto_main':
                self.run('GotoMain')
            elif command == 'restart_game':
                self.config.task_call('Restart')
            elif command == 'stop_game':
                self.device.app_stop()
            elif command == 'emulator_stop':
                self.device.emulator_stop()
                self._emulator_down = True
            elif command == 'emulator_start':
                from module.device.device import Device
                self.device = Device(self.config)
                self._emulator_down = False
            else:
                logger.warning(f'Agent: unknown command `{command}`')
        except Exception as e:
            logger.exception(f'Agent: command `{command}` failed: {e}')

    def _drain_agent_commands(self) -> None:
        """在任务边界消费命令队列；防止命令里再触发 drain 造成递归。"""
        if self.cmd_queue is None or self._in_agent_command:
            return
        self._in_agent_command = True
        try:
            while True:
                try:
                    command = self.cmd_queue.get_nowait()
                except Exception:
                    return
                logger.info(f'Agent: received command `{command}`')
                self._handle_agent_command(command)
        finally:
            self._in_agent_command = False

    def _publish_frame_once(self) -> None:
        """把当前帧发布给父进程；只读 device.image，不触发截图。"""
        if self.frame_queue is None:
            return
        device = self.__dict__.get('device')
        if device is None:
            return
        image = getattr(device, 'image', None)
        if image is None:
            return
        try:
            import time
            import cv2
            height, width = image.shape[:2]
            scale = min(1.0, self._FRAME_MAX_EDGE / max(height, width))
            if scale < 1.0:
                image = cv2.resize(image, (int(width * scale), int(height * scale)))
            ok, buffer = cv2.imencode(
                '.jpg', cv2.cvtColor(image, cv2.COLOR_RGB2BGR),
                [int(cv2.IMWRITE_JPEG_QUALITY), int(self._FRAME_QUALITY)],
            )
            if not ok:
                return
            try:
                self.frame_queue.get_nowait()
            except Exception:
                pass
            self.frame_queue.put_nowait((buffer.tobytes(), time.time()))
        except Exception as e:
            logger.debug(f'Agent: publish frame failed: {e}')

    def _start_frame_publisher(self) -> None:
        if self.frame_queue is None:
            return
        import time

        def run():
            while True:
                time.sleep(self._FRAME_INTERVAL)
                self._publish_frame_once()

        Thread(target=run, name='AgentFramePublisher', daemon=True).start()
```

在 `loop()` 的 `while 1:` 循环体最前面加两行：

```python
        while 1:
            self._drain_agent_commands()
            if date.today() > start_day:
```

并在 `loop()` 开头、`with _log_switch_lock:` 之后加：

```python
        self._start_frame_publisher()
```

在 `wait_until()` 的 `time.sleep(5)` 之后加：

```python
            time.sleep(5)
            self._drain_agent_commands()
```

在 `_try_acquire_queue_token()` 的 `time.sleep(30)` 之后加：

```python
            time.sleep(30)
            self._drain_agent_commands()
```

- [ ] **Step 4: 改 `module/server/script_process.py`**

`ScriptProcess.__init__` 追加：

```python
        self.cmd_queue = multiprocessing.Queue()
        self.frame_queue = multiprocessing.Queue(maxsize=1)
```

新增方法：

```python
    def is_alive(self) -> bool:
        """父进程视角：子进程是否还活着。"""
        return bool(self._process is not None and self._process.is_alive())
```

`start()` 里的 `multiprocessing.Process(...)` 调用改为传队列：

```python
        self._process = multiprocessing.Process(target=func,
                                               args=(self.config_name, self.state_queue,
                                                     self.log_pipe_in, self.cmd_queue,
                                                     self.frame_queue,),
                                               name=self.config_name,
                                               daemon=True
                                               )
```

`func` 签名改为：

```python
def func(config: str, state_queue: multiprocessing.Queue, log_pipe_in,
         cmd_queue=None, frame_queue=None) -> None:
```

`func` 内部构造 Script 改为：

```python
        from script import Script
        script = Script(config_name=config, cmd_queue=cmd_queue, frame_queue=frame_queue)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/agent/test_script_agent_hooks.py -v`
Expected: PASS，8 passed

- [ ] **Step 6: 全量回归**

Run: `python -m pytest tests/agent -v`
Expected: PASS，全部通过

- [ ] **Step 7: 提交**

```bash
git add script.py module/server/script_process.py tests/agent/test_script_agent_hooks.py
git commit -m "feat(agent): command queue and frame publisher in script subprocess"
```

---

### Task 10: 运行手册、playbook 与端到端冒烟

**Files:**
- Create: `docs/agent/daily-playbook.md`
- Create: `docs/agent/README.md`
- Create: `scripts/agent_smoke.py`（仓库根下 `scripts/` 为新增目录）
- Test: `tests/agent/test_smoke_dry_run.py`

**Interfaces:**
- Consumes: Task 5–9 全部
- Produces: 可复制的 DSH 配置片段与巡检提示词；`scripts/agent_smoke.py` 支持 `--dry-run`

- [ ] **Step 1: 写 playbook**

创建 `docs/agent/daily-playbook.md`：

```markdown
# 每日巡检 playbook

你负责让 OAS 把今天的阴阳师日常跑完。你不是开发者，**不要改任何源码**，也不要试图修 bug。

## 每一轮做什么

1. 调 `get_briefing`，读 `plan`（人写的意图）、`state`（今天已经做了什么）、`hold`。
2. `hold.active` 为真就直接结束本轮，只回一句「人在玩，已让位」。
3. 对照 `plan.must`：谁今天还没做完，就把谁 `run_task_now(position="front")`。
4. 看 `list_instances`：
   - 有实例 `INACTIVE` 且现在不在 `quiet_hours` 内 → `set_instance_running(true)`。
   - 实例 `WARNING` → `get_log` 看最后 80 行，再决定重跑还是跳过。
5. 每做完一件事，`update_daily_state` 记一笔、`log_decision` 记一句理由。
6. 本轮没有该做的事就结束，不要空转。

## 失败处理（硬规则）

- 同一个任务连续失败 **2** 次（`plan.on_failure.retry`）→ 记进 `state.skipped`、`file_issue`、继续下一个。
- **绝不**因为一个任务卡住而反复重试或等待。
- 同一实例连续 3 个不同任务失败 → `emergency_stop`，`file_issue` 说明情况，结束本轮。

## 语言与输出

- 用中文汇报，一次不超过 10 行。
- 报告格式：`日期 时间 | 做了什么 | 结果 | 下一步`。
```

- [ ] **Step 2: 写运行手册**

创建 `docs/agent/README.md`：

```markdown
# OAS Agent 接入说明

## 1. 启动 OAS 后端

```bash
python server.py --port 22267
```

`/mcp` 与 `/api/agent/*` 只接受本机访问。

## 2. 把 MCP 挂到 DSH

写入 `$DSH_HOME/cordis.patch.yml`（Windows 默认 `%USERPROFILE%\.dsh\cordis.patch.yml`），或启动时 `--patch` 指向一个文件：

```yaml
- insert:
    - id: mcp-oas
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: oas
        transport: streamable-http
        url: http://127.0.0.1:22267/mcp
        toolCallTimeoutMs: 30000
```

改完必须重启 DSH，工具才会以 `mcp__oas__*` 出现。

## 3. 每日计划

```bash
mkdir -p data/agent
cp config/agent/daily_plan.example.yaml data/agent/daily_plan.yaml
```

编辑 `must` / `optional` / `protected_tasks` / `wake`。

## 4. 两种拉起方式

- **常驻**：在一个专用会话里让 agent 建一个每 15 分钟的提醒做巡检。
- **headless**：Windows 任务计划执行
  `dsh --profile headless "按 docs/agent/daily-playbook.md 巡检一轮，先调 mcp__oas__get_briefing"`。

工作目录固定为 `data/agent/workspace/`：它写不到 `module/`，改不了源码。

## 5. 人在玩的时候

- 聊天里说「让位」，agent 调 `hold_device`；
- 或者浏览器打开 `http://127.0.0.1:22267/api/agent/hold?minutes=120`（POST）；
- 急停：`POST http://127.0.0.1:22267/api/agent/emergency_stop`。

## 6. 出问题看哪里

- `data/agent/journal/<date>.md`：agent 的决策理由；
- `data/agent/issues/ISS-*.md`：阻塞性问题，含快照、日志片段、截图；
- `data/agent/state.json`：今天做到哪了。
```

- [ ] **Step 3: 写冒烟脚本**

创建 `scripts/agent_smoke.py`：

```python
# This Python file uses the following encoding: utf-8
"""对 /api/agent/* 做一轮只读冒烟，不触碰设备。

用法: python scripts/agent_smoke.py --base http://127.0.0.1:22267
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def fetch(base: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base}{path}", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:22267")
    args = parser.parse_args()

    checks = []
    try:
        instances = fetch(args.base, "/api/agent/instances")
        checks.append(("instances", isinstance(instances, list) and len(instances) > 0))
        if instances:
            name = instances[0]["name"]
            snapshot = fetch(args.base, f"/api/agent/{name}/snapshot")
            checks.append(("snapshot", snapshot.get("name") == name))
            tasks = fetch(args.base, f"/api/agent/{name}/tasks")
            checks.append(("tasks", isinstance(tasks, list) and len(tasks) > 0))
            log = fetch(args.base, f"/api/agent/{name}/log?limit=5")
            checks.append(("log", "lines" in log))
        briefing = fetch(args.base, "/api/agent/briefing")
        checks.append(("briefing", "plan" in briefing and "hold" in briefing))
    except urllib.error.URLError as error:
        print(f"FAIL cannot reach {args.base}: {error}")
        return 1

    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 写脚本自身的测试**

创建 `tests/agent/test_smoke_dry_run.py`：

```python
import importlib.util
from pathlib import Path


def test_smoke_module_imports():
    path = Path("scripts/agent_smoke.py")
    assert path.exists()
    spec = importlib.util.spec_from_file_location("agent_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "fetch")
    assert callable(module.main)


def test_playbook_and_readme_exist():
    assert Path("docs/agent/daily-playbook.md").exists()
    assert Path("docs/agent/README.md").exists()
    assert "不要改任何源码" in Path("docs/agent/daily-playbook.md").read_text(encoding="utf-8")
```

- [ ] **Step 5: 运行测试**

Run: `python -m pytest tests/agent -v`
Expected: PASS，全部通过

- [ ] **Step 6: 真机冒烟（需要先启动 OAS）**

```bash
python server.py --port 22267
# 另一个终端
python scripts/agent_smoke.py
```

Expected: 每行 PASS；若有实例正在跑，`log` 应立即返回内容。

- [ ] **Step 7: 提交**

```bash
git add docs/agent scripts/agent_smoke.py tests/agent/test_smoke_dry_run.py
git commit -m "docs(agent): playbook, runbook and smoke script"
```

---

## 验收清单

- `python -m pytest tests/agent -v` 全绿。
- `python scripts/agent_smoke.py` 全 PASS。
- MCP 客户端能看到 21 个 `mcp__oas__*` 工具，`get_briefing` 可用。
- 非本机访问 `/api/agent/instances` 返回 403。
- `hold` 生效期间所有写操作返回 423；`emergency_stop` 停掉全部实例并进 hold。
- 受保护任务（`MysteryShop`）的写入返回 403 + `approval_token`。
- 真机：`run_task_now(front)` 后下一个任务边界立即执行；`screenshot` 能返回画面。
- 刷过不去的任务：`state.skipped` 有记录、`data/agent/issues/` 有对应文件。
