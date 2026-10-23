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
        for name, process in list(processes.items()):
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
        if thread is None:
            return
        if thread.is_alive():
            thread.join(timeout=2.0)
        if thread.is_alive():
            # 卡住的 poller 保留引用，让 start() 不会再造一个
            return
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
