"""pytest 全局夹具。

集成测试需要一个可写的 PostgreSQL，通过 TEST_DATABASE_URL 指向使用方本机已有实例；
没有配置数据库时自动跳过，单元测试仍然可跑。不要为本项目新启 Docker 容器。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

os.environ.setdefault("ENV", "test")
os.environ.setdefault("JWT_SECRET", "pytest-secret-123456789012345678901234567890")
os.environ.setdefault("CHECKPOINT_BACKEND", "memory")
os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("FAKE_LLM_DELAY_MS", "20")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
# M1.5：MCP 测试用白名单放行 mock 主机；凭据加密密钥；附件目录隔离到测试库临时区。
os.environ.setdefault("MCP_ALLOWED_HOSTS", "mcp.example.com,mcp.test")
os.environ.setdefault("CAPABILITY_SECRET_KEY", "pytest-capability-secret-key-0123456789abcdef")
os.environ.setdefault("ATTACHMENT_DIR", "data/test-attachments")
os.environ.setdefault("APPROVAL_TIMEOUT_SECONDS", "3")
# M2：A2A 测试用白名单放行 mock 主机；缩短客户端超时便于超时场景测试。
os.environ.setdefault("A2A_ALLOWED_HOSTS", "a2a.example.com,a2a.test,a2a-slow.test")
os.environ.setdefault("A2A_CLIENT_TIMEOUT_SECONDS", "5")
os.environ.setdefault("A2A_INPUT_TIMEOUT_SECONDS", "30")
# M4：知识库测试强制本地存储与假向量，避免读到本机 .env 的真实存储/Embedding 配置。
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ.setdefault("EMBEDDING_PROVIDER", "fake")
os.environ.setdefault("EMBEDDING_API_KEY", "")
os.environ.setdefault("KNOWLEDGE_DIR", "data/test-knowledge")

import httpx
import pytest
from sqlalchemy import text

from app.agents.checkpointer import reset_memory_checkpointer
from app.core.config import reset_settings_cache
from app.core.rate_limit import limiter
from app.db import models  # noqa: F401  # 注册全部模型
from app.db.base import Base
from app.db.session import dispose_engine, get_engine
from app.main import app
from app.modules.chat.registry import registry
from app.modules.mcp.service import clear_test_transports

reset_settings_cache()

_TABLES = (
    "knowledge_chunks, knowledge_documents, a2a_tasks, remote_agents, skill_files, "
    "mcp_tools, user_capability_overrides, skills, "
    "mcp_servers, attachments, memories, tool_invocations, run_events, approvals, runs, "
    "messages, conversations, refresh_tokens, system_settings, user_settings, users, audit_logs"
)


@pytest.fixture
async def db() -> AsyncIterator[None]:
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("需要可用的 PostgreSQL：把 TEST_DATABASE_URL 指向本机已有数据库")
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with get_engine().begin() as conn:
        await conn.execute(text(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE"))
    reset_memory_checkpointer()
    limiter.reset()
    clear_test_transports()
    from app.modules.a2a.client import clear_test_transports as clear_a2a_transports

    clear_a2a_transports()
    await registry.shutdown()
    shutil.rmtree(Path("data/test-attachments"), ignore_errors=True)


@pytest.fixture
async def client(db: None) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http
    await dispose_engine()


@pytest.fixture
async def second_client(client: httpx.AsyncClient) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def register_user(
    client: httpx.AsyncClient, *, email: str | None = None, password: str = "password123"
) -> dict:
    email = email or f"user-{uuid.uuid4().hex[:8]}@example.com"
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "display_name": "测试用户"},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    payload["password"] = password
    return payload


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def collect_sse(
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str],
    payload: dict,
    stop_events: tuple[str, ...] = ("message.done", "error"),
) -> tuple[str | None, list[tuple[str, dict]]]:
    """读取 SSE 直到终态事件，返回 run_id 与事件列表。"""
    events: list[tuple[str, dict]] = []
    async with client.stream(
        "POST", "/api/v1/chat/stream", json=payload, headers=headers
    ) as response:
        assert response.status_code == 200, await response.aread()
        assert response.headers["content-type"].startswith("text/event-stream")
        run_id = response.headers.get("X-Run-ID")
        event_name: str | None = None
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event_name:
                data = json.loads(line.split(":", 1)[1].strip())
                events.append((event_name, data))
                if event_name in stop_events:
                    return run_id, events
    return run_id, events


async def wait_for_event(
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str],
    payload: dict,
    wanted: str,
    timeout: float = 10.0,
) -> tuple[str | None, list[tuple[str, dict]], httpx.Response | None]:
    """打开 SSE 直到出现指定事件；返回已读事件与打开的连接（用于取消场景）。"""
    events: list[tuple[str, dict]] = []
    async with asyncio.timeout(timeout):
        async with client.stream(
            "POST", "/api/v1/chat/stream", json=payload, headers=headers
        ) as response:
            run_id = response.headers.get("X-Run-ID")
            event_name: str | None = None
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and event_name:
                    events.append((event_name, json.loads(line.split(":", 1)[1].strip())))
                    if event_name == wanted:
                        return run_id, events, response
    return run_id, events, None


async def collect_run_events_live(
    run_id: str,
    *,
    stop_on: set[str] | None = None,
    on_event=None,
    timeout: float = 30.0,
) -> list[tuple[str, dict]]:
    """服务层实时读取运行事件（绕过 ASGITransport 的整段缓冲）。

    ``on_event(name, data)`` 在读到每个事件后调用，可用于审批决定等中途交互；
    返回读到的事件列表（遇到 stop_on 中的事件即停止）。
    """
    from app.modules.chat.service import stream_run_events

    stop_on = stop_on or {"message.done", "error"}
    events: list[tuple[str, dict]] = []
    async with asyncio.timeout(timeout):
        async for frame in stream_run_events(uuid.UUID(run_id)):
            event_name: str | None = None
            for line in frame.strip().split("\n"):
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and event_name:
                    data = json.loads(line.split(":", 1)[1].strip())
                    events.append((event_name, data))
                    if on_event is not None:
                        await on_event(event_name, data)
                    if event_name in stop_on:
                        return events
    return events
