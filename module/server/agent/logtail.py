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
