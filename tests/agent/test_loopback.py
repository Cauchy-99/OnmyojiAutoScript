import asyncio

from module.server.agent.app_factory import LoopbackOnlyMiddleware


def _run(scope, inner_status=200):
    sent = []

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": inner_status, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    middleware = LoopbackOnlyMiddleware(inner)
    asyncio.run(middleware(scope, receive, send))
    return sent[0]["status"]


def _scope(path, host):
    return {"type": "http", "method": "GET", "path": path, "headers": [], "client": (host, 1234)}


def test_loopback_is_allowed():
    assert _run(_scope("/api/agent/instances", "127.0.0.1")) == 200


def test_ipv6_loopback_is_allowed():
    assert _run(_scope("/api/agent/instances", "::1")) == 200


def test_lan_client_is_rejected():
    assert _run(_scope("/api/agent/instances", "192.168.1.20")) == 403


def test_mcp_path_is_protected():
    assert _run(_scope("/mcp", "10.0.0.5")) == 403


def test_normal_oas_path_is_not_protected():
    assert _run(_scope("/config_all", "10.0.0.5")) == 200


def test_install_agent_registers_the_guard(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from module.server.agent.app_factory import install_agent
    from module.server.agent.logtail import LogTailer
    from module.server.agent.memory import AgentMemory
    from module.server.agent.service import AgentService
    from tests.agent.fakes import FakeManager

    service = AgentService(
        FakeManager(), memory=AgentMemory(root=tmp_path), tailer=LogTailer(root=tmp_path)
    )
    app = FastAPI()
    install_agent(app, service=service, mount_mcp=False)
    client = TestClient(app)

    # TestClient's default client host is "testclient", which is not loopback.
    assert client.get("/api/agent/instances").status_code == 403


def test_install_agent_leaves_other_paths_alone(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from module.server.agent.app_factory import install_agent
    from module.server.agent.logtail import LogTailer
    from module.server.agent.memory import AgentMemory
    from module.server.agent.service import AgentService
    from tests.agent.fakes import FakeManager

    service = AgentService(
        FakeManager(), memory=AgentMemory(root=tmp_path), tailer=LogTailer(root=tmp_path)
    )
    app = FastAPI()
    install_agent(app, service=service, mount_mcp=False)
    client = TestClient(app)

    assert client.get("/api/agent/instances").status_code == 403
    assert client.get("/api/agentfoo").status_code == 404
    assert client.get("/home_menu").status_code == 404


def test_protected_prefix_is_not_a_bare_prefix_match():
    from module.server.agent.app_factory import _is_protected

    assert _is_protected("/api/agent") is True
    assert _is_protected("/api/agent/instances") is True
    assert _is_protected("/mcp") is True
    assert _is_protected("/mcp/anything") is True
    assert _is_protected("/api/agentfoo") is False
    assert _is_protected("/mcpfoo") is False


def test_install_agent_survives_missing_mcp(tmp_path, monkeypatch):
    import sys

    from fastapi import FastAPI

    from module.server.agent.app_factory import install_agent
    from module.server.agent.logtail import LogTailer
    from module.server.agent.memory import AgentMemory
    from module.server.agent.service import AgentService
    from tests.agent.fakes import FakeManager

    monkeypatch.setitem(sys.modules, "module.server.mcp_server", None)

    service = AgentService(
        FakeManager(), memory=AgentMemory(root=tmp_path), tailer=LogTailer(root=tmp_path)
    )
    app = FastAPI()

    returned = install_agent(app, service=service, mount_mcp=True)

    assert returned is service
    assert getattr(app.state, "agent_mcp", None) is None
    assert not any(getattr(route, "path", "").startswith("/mcp") for route in app.routes)
