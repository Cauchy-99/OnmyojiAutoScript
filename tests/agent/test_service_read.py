import queue
from types import SimpleNamespace

from module.server.agent.bridge import FrameStore
from module.server.agent.logtail import LogTailer
from module.server.agent.memory import AgentMemory
from module.server.agent.service import AgentService, UnknownInstance
from tests.agent.fakes import FakeConfig, FakeManager


class SchedulerConfig(FakeConfig):
    """FakeConfig.update_scheduler 只填 _pending；真实 Config 还暴露 pending_task/waiting_task。"""

    def update_scheduler(self):
        super().update_scheduler()
        self.pending_task = [
            SimpleNamespace(command=item["name"], next_run=item["next_run"])
            for item in getattr(self, "_pending", [])
        ]
        self.waiting_task = []


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


def test_snapshot_reports_running_and_every_pending_task(tmp_path):
    manager = FakeManager(configs={"oas1": SchedulerConfig("oas1")})
    data = manager.config_cache("oas1").data
    data["running_task"] = "Exploration"
    data["exploration"]["scheduler"].update({"enable": True, "next_run": "2026-10-23 18:00:00"})
    data["mystery_shop"]["scheduler"].update({"enable": True, "next_run": "2026-10-23 19:00:00"})
    data["restart"]["scheduler"].update({"enable": True, "next_run": "2026-10-23 20:00:00"})

    schedule = _service(tmp_path, manager).snapshot("oas1")["schedule"]

    assert schedule["running"]["name"] == "exploration"
    pending_names = [item["name"] for item in schedule["pending"]]
    assert "exploration" not in pending_names
    # FakeConfig.update_scheduler 按插入顺序排队，不按 next_run 排序（真实调度器会排序），
    # 所以这里比较集合；再确认三个任务各出现且只出现一次。
    assert sorted(pending_names) == ["mystery_shop", "restart"]
    assert sorted([schedule["running"]["name"], *pending_names]) == [
        "exploration",
        "mystery_shop",
        "restart",
    ]


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
    service.frame_store.update("oas1", b"jpeg-bytes", ts=1000.0)
    payload = service.screenshot("oas1")
    assert payload["available"] is True
    assert payload["image"] == b"jpeg-bytes"


def test_briefing_contains_plan_state_and_hold(tmp_path):
    payload = _service(tmp_path).briefing()
    assert payload["instances"][0]["name"] == "oas1"
    assert payload["plan"]["timezone"] == "Asia/Shanghai"
    assert "instances" in payload["state"]
    assert payload["hold"]["active"] is False


def test_screenshot_is_per_instance(tmp_path):
    manager = FakeManager(names=("oas1", "oas2"))
    service = _service(tmp_path, manager)
    service.frame_store.update("oas1", b"one", ts=1000.0)

    assert service.screenshot("oas1")["available"] is True
    assert service.screenshot("oas2")["available"] is False


def test_list_tasks_keeps_missing_intervals_null(tmp_path):
    manager = FakeManager()
    scheduler = manager.config_cache("oas1").data["exploration"]["scheduler"]
    scheduler.pop("success_interval")
    scheduler.pop("failure_interval")

    exploration = next(
        item for item in _service(tmp_path, manager).list_tasks("oas1") if item["task"] == "exploration"
    )
    assert exploration["success_interval"] is None
    assert exploration["failure_interval"] is None
