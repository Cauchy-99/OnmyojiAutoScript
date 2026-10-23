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
