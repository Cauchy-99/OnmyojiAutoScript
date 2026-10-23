import asyncio
import json

from module.server.agent.logtail import LogTailer
from module.server.agent.memory import AgentMemory
from module.server.agent.service import AgentService
from module.server.mcp_server import TOOL_NAMES, create_mcp
from tests.agent.fakes import FakeManager


def _mcp(tmp_path, manager=None):
    manager = manager or FakeManager()
    service = AgentService(
        manager, memory=AgentMemory(root=tmp_path), tailer=LogTailer(root=tmp_path)
    )
    return create_mcp(service), manager


def test_tool_names_are_complete():
    assert len(TOOL_NAMES) == 21
    assert "get_briefing" in TOOL_NAMES
    assert "file_issue" in TOOL_NAMES


def test_list_tools_exposes_expected_names(tmp_path):
    mcp, _ = _mcp(tmp_path)

    async def collect():
        return await mcp.list_tools()

    tools = asyncio.run(collect())
    assert {tool.name for tool in tools} == set(TOOL_NAMES)


def test_call_tool_list_instances_returns_json_text(tmp_path):
    mcp, _ = _mcp(tmp_path)

    async def call():
        result = await mcp.call_tool("list_instances", {})
        return result

    content, structured = asyncio.run(call())
    text = content[0].text
    assert json.loads(text)[0]["name"] == "oas1"


def test_call_tool_set_task_enabled(tmp_path):
    mcp, manager = _mcp(tmp_path)

    async def call():
        return await mcp.call_tool("set_task_enabled", {"instance": "oas1", "task": "Exploration", "enabled": True})

    content, _ = asyncio.run(call())
    payload = json.loads(content[0].text)
    assert payload["ok"] is True
    assert manager.config_cache("oas1").data["exploration"]["scheduler"]["enable"] is True


def test_call_tool_screenshot_without_frame_is_diagnostic(tmp_path):
    mcp, _ = _mcp(tmp_path)

    async def call():
        return await mcp.call_tool("screenshot", {"instance": "oas1"})

    blocks = asyncio.run(call())
    assert "no frame" in blocks[0].text.lower()


def test_screenshot_returns_image_content(tmp_path):
    import time

    manager = FakeManager()
    service = AgentService(
        manager, memory=AgentMemory(root=tmp_path), tailer=LogTailer(root=tmp_path)
    )
    service.frame_store.update("oas1", b"\xff\xd8jpeg", ts=time.time())
    mcp = create_mcp(service)

    async def call():
        return await mcp.call_tool("screenshot", {"instance": "oas1"})

    blocks = asyncio.run(call())
    assert isinstance(blocks, list)
    assert blocks[0].type == "image"


def test_tool_error_surfaces_not_swallowed(tmp_path):
    mcp, _ = _mcp(tmp_path)

    async def call():
        return await mcp.call_tool("get_snapshot", {"instance": "nope"})

    try:
        asyncio.run(call())
    except Exception as error:  # ToolError from the SDK
        assert "nope" in str(error)
    else:
        raise AssertionError("expected a tool error")


def test_run_task_now_forwards_the_approval_token(tmp_path):
    from module.server.agent.logtail import LogTailer
    from module.server.agent.memory import AgentMemory
    from module.server.agent.service import AgentService
    from module.server.mcp_server import create_mcp
    from tests.agent.fakes import FakeManager

    manager = FakeManager()
    service = AgentService(
        manager, memory=AgentMemory(root=tmp_path), tailer=LogTailer(root=tmp_path)
    )
    captured = {}

    async def spy(instance, task, position="front", approval_token=None):
        captured["approval_token"] = approval_token
        return {"ok": True}

    service.run_now = spy
    mcp = create_mcp(service)

    async def call():
        return await mcp.call_tool(
            "run_task_now",
            {"instance": "oas1", "task": "MysteryShop", "approval_token": "tok-123"},
        )

    content, _ = asyncio.run(call())
    assert captured["approval_token"] == "tok-123"
    assert '"ok": true' in content[0].text.lower()
