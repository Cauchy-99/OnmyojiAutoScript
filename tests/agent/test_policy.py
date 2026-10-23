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
