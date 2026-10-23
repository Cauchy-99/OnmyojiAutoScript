# This Python file uses the following encoding: utf-8
"""增量读取 ./log/<date>_<name>.txt。

游标语义：
- `cursor=None`（或越界）→ 返回文件末尾最多 `limit` 行；起点吸附到行首，因此不会从
  半个 UTF-8 字符或半行开始。窗口不够 `limit` 行时继续向前扩，只有整个文件更短时才会
  少于 `limit` 行。
- `cursor=N` → 从第 N 字节开始读新增内容；结果仍最多 `limit` 行，超出时只保留最新的部分
  并把 `truncated` 置为 True。
- 返回的 `cursor` 永远是文件当前大小；`eof` 目前恒为 True（日志是追加写入的普通文件）。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Callable

_BYTES_PER_LINE_GUESS = 400


def _tail_start(data: bytes, size: int, limit: int) -> int:
    """算出一个行首偏移，使 data[start:] 至少包含 limit 行（除非整个文件更短）。"""
    start = max(0, size - limit * _BYTES_PER_LINE_GUESS)
    while start > 0 and data.count(b"\n", start, size) <= limit:
        earlier = max(0, start - limit * _BYTES_PER_LINE_GUESS)
        if earlier == start:
            break
        start = earlier
    if start > 0 and data[start - 1] != 0x0A:
        newline = data.find(b"\n", start)
        # 窗口里一个换行都没有（单行超长）时退回文件头，宁可多读也不丢数据
        start = newline + 1 if newline != -1 else 0
    return start


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
        if cursor is None or cursor < 0 or cursor > size:
            start = _tail_start(data, size, limit)
            lines = data[start:].decode("utf-8", errors="replace").splitlines()
        else:
            lines = data[cursor:].decode("utf-8", errors="replace").splitlines()
            if lines and lines[0] == "":
                lines = lines[1:]

        truncated = len(lines) > limit
        if truncated:
            lines = lines[-limit:]
        return {
            "lines": lines,
            "cursor": size,
            "eof": True,
            "path": str(path),
            "truncated": truncated,
        }
