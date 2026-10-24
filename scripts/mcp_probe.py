# This Python file uses the following encoding: utf-8
"""对运行中的 OAS 做一次真实 MCP 握手与只读调用。

用法:
    python scripts/mcp_probe.py                       # 默认 http://127.0.0.1:22267/mcp
    python scripts/mcp_probe.py --url http://127.0.0.1:22267/mcp

只做 initialize / tools/list / get_briefing / list_instances，不写任何状态。
退出码 0 表示全部通过。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

EXPECTED_TOOLS = 20


async def probe(url: str) -> int:
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("PASS initialize")

            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            print(f"tools={len(names)}")
            print(" ", ", ".join(names))
            if len(names) != EXPECTED_TOOLS:
                print(f"FAIL expected {EXPECTED_TOOLS} tools, got {len(names)}")
                return 1
            if "release_device" in names:
                print("FAIL release_device must not be exposed to the agent")
                return 1
            print("PASS tools/list")

            briefing = await session.call_tool("get_briefing", {})
            text = "".join(block.text for block in briefing.content if getattr(block, "text", None))
            payload = json.loads(text)
            for key in ("plan", "state", "hold", "instances"):
                if key not in payload:
                    print(f"FAIL briefing missing `{key}`")
                    return 1
            print("PASS get_briefing")

            instances = await session.call_tool("list_instances", {})
            text = "".join(block.text for block in instances.content if getattr(block, "text", None))
            rows = json.loads(text)
            print(f"PASS list_instances ({len(rows)} instance(s))")
            for row in rows:
                print(f"  {row['name']}: {row['state']} alive={row['pid_alive']} task={row.get('running_task') or '-'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:22267/mcp")
    args = parser.parse_args()
    try:
        return asyncio.run(probe(args.url))
    except Exception as error:  # noqa: BLE001
        print(f"FAIL {type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
