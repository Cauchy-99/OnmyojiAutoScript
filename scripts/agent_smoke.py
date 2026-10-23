# This Python file uses the following encoding: utf-8
"""对 /api/agent/* 做一轮只读冒烟，不触碰设备。

用法: python scripts/agent_smoke.py --base http://127.0.0.1:22267
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def fetch(base: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base}{path}", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:22267")
    args = parser.parse_args()

    checks = []
    try:
        instances = fetch(args.base, "/api/agent/instances")
        checks.append(("instances", isinstance(instances, list) and len(instances) > 0))
        if instances:
            name = instances[0]["name"]
            snapshot = fetch(args.base, f"/api/agent/{name}/snapshot")
            checks.append(("snapshot", snapshot.get("name") == name))
            tasks = fetch(args.base, f"/api/agent/{name}/tasks")
            checks.append(("tasks", isinstance(tasks, list) and len(tasks) > 0))
            log = fetch(args.base, f"/api/agent/{name}/log?limit=5")
            checks.append(("log", "lines" in log))
        briefing = fetch(args.base, "/api/agent/briefing")
        checks.append(("briefing", "plan" in briefing and "hold" in briefing))
    except urllib.error.URLError as error:
        print(f"FAIL cannot reach {args.base}: {error}")
        return 1

    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
