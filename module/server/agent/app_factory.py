# This Python file uses the following encoding: utf-8
"""把 agent 能力层装进 FastAPI：路由、回环中间件、MCP 挂载。"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from module.logger import logger
from module.server.agent.service import AgentService
from module.server.agent_router import create_agent_router

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}
PROTECTED_PREFIXES = ("/api/agent", "/mcp")


def _is_protected(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in PROTECTED_PREFIXES)


class LoopbackOnlyMiddleware:
    """纯 ASGI 中间件：非回环地址访问受保护前缀一律 403。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and _is_protected(str(scope.get("path", ""))):
            client = scope.get("client")
            host = client[0] if client else None
            if host not in LOOPBACK_HOSTS:
                response = JSONResponse(
                    {"error": "forbidden", "detail": "agent endpoints are loopback-only"},
                    status_code=403,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_service(manager: Any) -> AgentService:
    return AgentService(manager)


def install_agent(
    app: FastAPI,
    service: AgentService | None = None,
    manager: Any = None,
    mount_mcp: bool = True,
) -> AgentService:
    if service is None:
        if manager is None:
            raise ValueError("install_agent needs either service or manager")
        service = build_service(manager)

    app.add_middleware(LoopbackOnlyMiddleware)
    app.include_router(create_agent_router(service))
    app.state.agent_service = service

    if mount_mcp:
        try:
            from module.server.mcp_server import create_mcp

            mcp = create_mcp(service)
        except ImportError as error:
            logger.warning(f'agent MCP endpoint disabled: {error}')
        else:
            app.state.agent_mcp = mcp
            app.mount("/mcp", mcp.streamable_http_app())
    return service
