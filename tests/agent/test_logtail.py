from datetime import date

from module.server.agent.logtail import LogTailer


def _tailer(tmp_path):
    return LogTailer(root=tmp_path, date_fn=lambda: date(2026, 10, 23))


def test_missing_log_returns_empty(tmp_path):
    result = _tailer(tmp_path).read("oas1")
    assert result["lines"] == []
    assert result["eof"] is True
    assert result["cursor"] == 0


def test_tail_without_cursor_returns_last_lines(tmp_path):
    log = tmp_path / "2026-10-23_oas1.txt"
    log.write_text("\n".join(f"line{i}" for i in range(10)), encoding="utf-8")

    result = _tailer(tmp_path).read("oas1", limit=3)
    assert result["lines"] == ["line7", "line8", "line9"]
    assert result["eof"] is True


def test_cursor_continuation_returns_only_new_lines(tmp_path):
    log = tmp_path / "2026-10-23_oas1.txt"
    log.write_text("a\nb\n", encoding="utf-8")
    tailer = _tailer(tmp_path)

    first = tailer.read("oas1")
    assert first["lines"] == ["a", "b"]

    with log.open("a", encoding="utf-8") as handle:
        handle.write("c\nd\n")

    second = tailer.read("oas1", cursor=first["cursor"])
    assert second["lines"] == ["c", "d"]
    assert second["cursor"] > first["cursor"]


def test_cursor_beyond_file_restarts_from_tail(tmp_path):
    log = tmp_path / "2026-10-23_oas1.txt"
    log.write_text("only\n", encoding="utf-8")
    result = _tailer(tmp_path).read("oas1", cursor=10_000)
    assert result["lines"] == ["only"]


def test_large_file_tail_starts_on_a_line_boundary(tmp_path):
    log = tmp_path / "2026-10-23_oas1.txt"
    lines = [f"第{i}行-任务开始" for i in range(2000)]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = _tailer(tmp_path).read("oas1", limit=5)
    assert len(result["lines"]) <= 5
    assert "\ufffd" not in "".join(result["lines"])
    assert all(line in lines for line in result["lines"])


def test_cursor_zero_is_capped_by_limit(tmp_path):
    log = tmp_path / "2026-10-23_oas1.txt"
    log.write_text("\n".join(f"line{i}" for i in range(10)), encoding="utf-8")

    result = _tailer(tmp_path).read("oas1", cursor=0, limit=3)
    assert result["lines"] == ["line7", "line8", "line9"]
    assert result["truncated"] is True
