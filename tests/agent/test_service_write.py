import asyncio
from datetime import date, datetime

import pytest

from module.server.agent.logtail import LogTailer
from module.server.agent.memory import AgentMemory
from module.server.agent.policy import HoldActive, NeedsApproval
from module.server.agent.service import AgentService
from tests.agent.fakes import FakeManager, FakeProcess

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


def test_set_task_arg_tolerates_pydantic_coercion(tmp_path):
    manager = FakeManager()
    result = _service(tmp_path, manager).set_task_arg(
        "oas1", "Exploration", "scheduler", "enable", "true"
    )

    assert result["ok"] is True
    assert manager.config_cache("oas1").data["exploration"]["scheduler"]["enable"] is True


def test_emergency_stop_survives_a_broken_plan(tmp_path):
    (tmp_path / "daily_plan.yaml").write_text("wake: ['7:00']\n", encoding="utf-8")
    manager = FakeManager(names=("oas1",))
    service = _service(tmp_path, manager)
    with pytest.raises(ValueError):
        service.memory.plan()

    result = asyncio.run(service.emergency_stop())

    assert result["stopped"] == ["oas1"]
    assert result["hold"]["active"] is True


def test_emergency_stop_rejects_non_positive_minutes(tmp_path):
    manager = FakeManager(names=("oas1",))
    service = _service(tmp_path, manager)

    with pytest.raises(ValueError):
        asyncio.run(service.emergency_stop(minutes=0))

    assert manager.script_process["oas1"].stopped == 0


@pytest.mark.parametrize(
    "write",
    [
        pytest.param(lambda service: service.set_task_enabled("oas1", "Exploration", True), id="enable"),
        pytest.param(
            lambda service: service.set_task_arg("oas1", "Exploration", "scheduler", "priority", 3),
            id="args",
        ),
        pytest.param(
            lambda service: service.set_next_run("oas1", "Exploration", "2026-10-24 07:00:00"),
            id="next_run",
        ),
        pytest.param(lambda service: service.set_schedule_rule("oas1", "FIFO"), id="rule"),
        pytest.param(lambda service: service.send_command("oas1", "goto_main"), id="command"),
        pytest.param(lambda service: asyncio.run(service.run_now("oas1", "Exploration")), id="run_now"),
        pytest.param(lambda service: asyncio.run(service.set_running("oas2", True)), id="instance"),
    ],
)
def test_hold_blocks_every_gated_write(tmp_path, write):
    """hold 期间所有会碰配置/进程的写操作都必须被拒；update_daily_state/journal/file_issue 只写 data/agent，不在闸门内。"""
    manager = FakeManager(names=("oas1", "oas2"))
    service = _service(tmp_path, manager)
    service.hold(minutes=30, reason="playing")

    with pytest.raises(HoldActive):
        write(service)

    assert manager.config_cache("oas1").data["exploration"]["scheduler"]["enable"] is False
    assert manager.script_process["oas2"].started == 0
    assert manager.script_process["oas1"].cmd_queue.empty()


def test_hold_still_allows_release_and_emergency_stop(tmp_path):
    manager = FakeManager(names=("oas1",))
    service = _service(tmp_path, manager)
    service.hold(minutes=30)

    assert service.release_hold()["hold"]["active"] is False

    service.hold(minutes=30)
    result = asyncio.run(service.emergency_stop())
    assert result["stopped"] == ["oas1"]
    assert result["hold"]["active"] is True


def test_ungranted_approval_token_cannot_unlock_a_protected_write(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(NeedsApproval) as info:
        service.set_task_enabled("oas1", "MysteryShop", True)
    token = info.value.detail["approval_token"]

    with pytest.raises(NeedsApproval):
        service.set_task_enabled("oas1", "MysteryShop", True, approval_token=token)


class ExplodingProcess(FakeProcess):
    async def stop(self):
        self.stopped += 1
        raise RuntimeError("stop failed")


def test_emergency_stop_closes_the_gate_before_stopping(tmp_path):
    manager = FakeManager(names=("oas1",))
    manager.script_process["oas1"] = ExplodingProcess("oas1")
    service = _service(tmp_path, manager)

    result = asyncio.run(service.emergency_stop())

    assert result["ok"] is False
    assert result["failed"] == ["oas1"]
    assert result["hold"]["active"] is True


class GateProbeProcess(FakeProcess):
    def __init__(self, name, probe):
        super().__init__(name)
        self._probe = probe
        self.hold_during_stop = None

    async def stop(self):
        self.hold_during_stop = self._probe()
        self.stopped += 1


class CancellingProcess(FakeProcess):
    async def stop(self):
        raise asyncio.CancelledError


def test_emergency_stop_activates_the_gate_before_any_stop(tmp_path):
    manager = FakeManager(names=("oas1",))
    service = _service(tmp_path, manager)
    probe = GateProbeProcess("oas1", lambda: service.briefing()["hold"]["active"])
    manager.script_process["oas1"] = probe

    result = asyncio.run(service.emergency_stop())

    assert probe.hold_during_stop is True
    assert result["stopped"] == ["oas1"]
    assert result["hold"]["active"] is True


def test_emergency_stop_keeps_the_gate_on_cancellation(tmp_path):
    manager = FakeManager(names=("oas1",))
    service = _service(tmp_path, manager)
    manager.script_process["oas1"] = CancellingProcess("oas1")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.emergency_stop())

    assert service.briefing()["hold"]["active"] is True
