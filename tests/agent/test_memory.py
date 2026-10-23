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


def test_state_rolls_over_on_a_new_day(tmp_path):
    day_one = AgentMemory(root=tmp_path, now=lambda: datetime(2026, 10, 23, 18, 0, 0))
    day_one.update_state({"done": ["Exploration"], "attempts": {"Orochi": 2}})

    day_two = AgentMemory(root=tmp_path, now=lambda: datetime(2026, 10, 24, 7, 0, 0))
    fresh = day_two.state()

    assert fresh["date"] == "2026-10-24"
    assert fresh["done"] == []
    assert fresh["attempts"] == {}
    journal = (tmp_path / "journal" / "2026-10-24.md").read_text(encoding="utf-8")
    assert "跨天" in journal


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

    assert memory.check_approval(token, "task_args", "MysteryShop") is False
    assert memory.grant_approval(token) is True
    assert memory.check_approval(token, "task_args", "MysteryShop") is True
    assert memory.check_approval(token, "task_args", "RichMan") is False
    assert memory.consume_approval(token) is True
    assert memory.consume_approval(token) is False
    assert memory.grant_approval(token) is False


def test_plan_results_do_not_share_state(tmp_path):
    from module.server.agent.memory import DEFAULT_PLAN

    memory = _memory(tmp_path)
    first = memory.plan()
    first["must"].append("Exploration")
    first["budget"]["max_active_hours"] = 999

    second = memory.plan()
    assert second["must"] == []
    assert second["budget"]["max_active_hours"] == 10
    assert DEFAULT_PLAN["must"] == []
    assert DEFAULT_PLAN["budget"]["max_active_hours"] == 10


def test_set_issue_status_rejects_path_traversal(tmp_path):
    memory = _memory(tmp_path)
    try:
        memory.set_issue_status("../state", "resolved")
    except ValueError as error:
        assert "invalid issue id" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_set_issue_status_returns_false_without_status_marker(tmp_path):
    memory = _memory(tmp_path)
    path = tmp_path / "issues" / "ISS-20261023-181500-orochi.md"
    path.write_text("---\nid: ISS-20261023-181500-orochi\n---\n\n## 症状\nx\n", encoding="utf-8")

    assert memory.set_issue_status("ISS-20261023-181500-orochi", "resolved") is False
