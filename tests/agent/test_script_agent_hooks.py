"""Subprocess-side agent hooks in ``script.py``: command drain and frame publish.

``script.py`` imports the whole OAS runtime at module scope, and several of its
optional dependencies (``zerorpc``, ``adbutils``, ``ppocronnx`` ...) are not
installed in every environment. These tests never touch ``Config``/``Device``,
so the missing modules are stubbed only for the duration of the import and
removed again afterwards, keeping the rest of the test session unaffected.
"""
import importlib
import sys
import types
import warnings
from unittest.mock import MagicMock

_STUBBED = {}


def _install(name, module=None):
    sys.modules[name] = module if module is not None else MagicMock(name=name)
    _STUBBED[name] = name


try:
    importlib.import_module("cached_property")
except ImportError:
    # Keep real descriptor semantics for `Script.config` / `Script.device`.
    from functools import cached_property as _cached_property

    _shim = types.ModuleType("cached_property")
    _shim.cached_property = _cached_property
    _install("cached_property", _shim)

_OPTIONAL_MODULES = (
    "zerorpc",
    "zmq",
    "msgpack",
    "inflection",
    "onepush",
    "onepush.core",
    "onepush.exceptions",
    "onepush.providers",
    "onepush.providers.custom",
    "adbutils",
    "adbutils.errors",
    "uiautomator2",
    "anytree",
    "ppocronnx",
    "ppocronnx.predict_system",
)
for _name in _OPTIONAL_MODULES:
    try:
        importlib.import_module(_name)
    except Exception:
        _install(_name)

try:
    with warnings.catch_warnings():
        # Importing script.py pulls in the whole runtime, whose optional
        # dependencies emit unrelated deprecation warnings while importing.
        warnings.simplefilter("ignore")
        from script import Script
finally:
    for _name in _STUBBED:
        sys.modules.pop(_name, None)

import queue

import numpy as np
import pytest


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
