"""pytest 全局夹具。

集成测试需要一个可写的 PostgreSQL，通过 TEST_DATABASE_URL 指向使用方本机已有实例；
没有配置数据库时自动跳过，单元测试仍然可跑。不要为本项目新启 Docker 容器。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator

os.environ.setdefault("ENV", "test")
os.environ.setdefault("JWT_SECRET", "pytest-secret-123456789012345678901234567890")
os.environ.setdefault("CHECKPOINT_BACKEND", "memory")
os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("FAKE_LLM_DELAY_MS", "20")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

import httpx
import pytest
from sqlalchemy import text

from app.agents.checkpointer import reset_memory_checkpointer
from app.core.rate_limit import limiter
from app.db import models  # noqa: F401  # 注册全部模型
from app.db.base import Base
from app.db.session import dispose_engine, get_engine
from app.main import app
from app.modules.chat.registry import registry

_TABLES = (
    "run_events, approvals, runs, messages, conversations, "
    "refresh_tokens, system_settings, user_settings, users, audit_logs"
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
    await registry.shutdown()


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
