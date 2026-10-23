# This Python file uses the following encoding: utf-8
"""增量读取 ./log/<date>_<name>.txt。

游标语义：
- `cursor=None`（或越界）→ 返回文件末尾最多 `limit` 行；从文件末尾向前数换行符定位行首，
  因此不会从半个 UTF-8 字符或半行开始，耗时与文件大小成线性、与单行长度无关。起点不在
  文件头或行数超出 `limit` 时 `truncated` 为 True，表示还有更早的行没有返回。
- `cursor=N` → 从第 N 字节开始读新增内容；结果仍最多 `limit` 行，超出时只保留最新的部分
  并把 `truncated` 置为 True。
- 返回的 `cursor` 永远是文件当前大小；`eof` 目前恒为 True（日志是追加写入的普通文件）。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Callable


def _tail_start(data: bytes, size: int, limit: int) -> int:
    """返回倒数第 limit 行的行首偏移；不足 limit 行时返回 0。"""
    position = size
    for _ in range(limit + 1):
        newline = data.rfind(b"\n", 0, position)
        if newline == -1:
            return 0
        position = newline
    return position + 1


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
            return {
                "lines": [],
                "cursor": 0,
                "eof": True,
                "path": str(path),
                "truncated": False,
            }

        data = path.read_bytes()
        size = len(data)
        at_tail = cursor is None or cursor < 0 or cursor > size
        if at_tail:
            start = _tail_start(data, size, limit)
            lines = data[start:].decode("utf-8", errors="replace").splitlines()
        else:
            lines = data[cursor:].decode("utf-8", errors="replace").splitlines()
            if lines and lines[0] == "":
                lines = lines[1:]

        # 起点不在文件头，或窗口里行数超出 limit，都说明有更早的行没有返回
        truncated = len(lines) > limit
        if at_tail and start > 0:
            truncated = True
        if len(lines) > limit:
            lines = lines[-limit:]
        return {
            "lines": lines,
            "cursor": size,
            "eof": True,
            "path": str(path),
            "truncated": truncated,
        }
