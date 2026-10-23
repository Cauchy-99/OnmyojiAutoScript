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
