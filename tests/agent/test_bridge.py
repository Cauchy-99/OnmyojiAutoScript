import queue

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
    assert store.latest("oas1") == (None, None)

    store.update("oas1", b"jpg-1", ts=100.0)
    store.update("oas1", b"jpg-2", ts=101.0)
    payload, ts = store.latest("oas1")
    assert payload == b"jpg-2"
    assert ts == 101.0
    assert store.age("oas1", now=106.0) == 5.0


def test_frame_store_keeps_instances_separate():
    store = FrameStore()
    store.update("oas1", b"a", ts=1.0)
    store.update("oas2", b"b", ts=2.0)

    assert store.latest("oas1") == (b"a", 1.0)
    assert store.latest("oas2") == (b"b", 2.0)
    assert store.latest("oas3") == (None, None)
    assert store.age("oas3") is None


def test_collector_drains_queue_and_keeps_newest():
    manager = FakeManager()
    proc = manager.script_process["oas1"]
    proc.frame_queue.put((b"one", 10.0))
    store = FrameStore()
    collector = FrameCollector(manager, store)

    collector.poll_once()
    assert store.latest("oas1") == (b"one", 10.0)

    proc.frame_queue.put((b"two", 11.0))
    collector.poll_once()
    assert store.latest("oas1") == (b"two", 11.0)


def test_collector_ignores_empty_and_missing_queue():
    manager = FakeManager()
    del manager.script_process["oas1"].frame_queue
    store = FrameStore()
    FrameCollector(manager, store).poll_once()
    assert store.latest("oas1") == (None, None)


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
    first = collector._thread
    collector.start()
    assert collector._thread is first

    collector.stop()
    assert collector._thread is None
    collector.stop()
    assert collector._thread is None


def test_collector_drains_a_backlog_and_keeps_newest():
    manager = FakeManager()
    process = manager.script_process["oas1"]
    process.frame_queue = queue.Queue(maxsize=10)
    for index in range(3):
        process.frame_queue.put((f"frame-{index}".encode(), float(index)))

    store = FrameStore()
    FrameCollector(manager, store).poll_once()

    assert store.latest("oas1") == (b"frame-2", 2.0)
    assert process.frame_queue.empty()


class StuckThread:
    """一个永远存活、join 不返回的桩线程。"""

    def __init__(self):
        self.joins = 0

    def is_alive(self):
        return True

    def join(self, timeout=None):
        self.joins += 1


class MutatingQueue:
    """第一次取帧时增删实例表，模拟生产环境的并发改动。"""

    def __init__(self, manager, name="oas2"):
        self.manager = manager
        self.name = name
        self.calls = 0

    def get_nowait(self):
        if self.calls == 0:
            self.manager.script_process[self.name] = object()
        else:
            self.manager.script_process.pop(self.name, None)
        self.calls += 1
        raise queue.Empty


def test_stop_keeps_a_stuck_poller_and_start_does_not_double_it():
    collector = FrameCollector(FakeManager(), FrameStore(), interval=0.01)
    stuck = StuckThread()
    collector._thread = stuck

    collector.stop()
    assert collector._thread is stuck

    collector.start()
    assert collector._thread is stuck


def test_poll_once_survives_instance_map_mutation():
    manager = FakeManager()
    manager.script_process["oas1"].frame_queue = MutatingQueue(manager)
    store = FrameStore()

    FrameCollector(manager, store).poll_once()

    assert store.latest("oas1") == (None, None)
