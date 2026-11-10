"""pytest 全局夹具。

集成测试需要一个可写的 PostgreSQL，通过 TEST_DATABASE_URL 指向使用方本机已有实例；
没有配置数据库时自动跳过，单元测试仍然可跑。不要为本项目新启 Docker 容器。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

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
# 这里必须硬赋值而不是 setdefault：开发者 shell 里导出的同名变量会让测试写 S3 或发真实
# Embedding 网络请求。DATABASE_URL 仍用 setdefault，以支持 TEST_DATABASE_URL 覆盖。
os.environ["STORAGE_BACKEND"] = "local"
# 语义组评测逃生舱：仅当 KNOWLEDGE_EVAL_REAL=1 时放开 provider/key，让 .env 或 shell
# 里的真实 Embedding 配置生效；否则保持硬赋值 fake + 空 key 的零网络基线。
# 风险：置 1 后整套测试都会真实访问 Embedding 服务、不再零网络，只允许手动跑离线评测时
# 使用，例如：
#   KNOWLEDGE_EVAL_REAL=1 TEST_DATABASE_URL=... \
#     .venv/bin/python -m pytest tests/integration/test_knowledge_eval.py -k semantic -v
if os.environ.get("KNOWLEDGE_EVAL_REAL") != "1":
    os.environ["EMBEDDING_PROVIDER"] = "fake"
    os.environ["EMBEDDING_API_KEY"] = ""
os.environ["KNOWLEDGE_DIR"] = "data/test-knowledge"

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.checkpointer import reset_memory_checkpointer
from app.core.config import get_settings, reset_settings_cache
from app.core.rate_limit import limiter
from app.core.security import create_access_token, hash_password
from app.core.storage import LocalFileStorage, reset_storage
from app.db import models  # noqa: F401  # 注册全部模型
from app.db.base import Base
from app.db.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.db.models.user import User
from app.db.session import dispose_engine, get_engine, get_session_factory
from app.main import app
from app.modules.chat.registry import registry
from app.modules.knowledge.embedding import FakeEmbeddingProvider, reset_embedding_provider
from app.modules.knowledge.splitting import split_text
from app.modules.mcp.service import clear_test_transports

reset_settings_cache()

_TABLES = (
    "knowledge_chunks, knowledge_documents, a2a_tasks, remote_agents, skill_files, "
    "mcp_tools, user_capability_overrides, skills, "
    "mcp_servers, attachments, memories, tool_invocations, run_events, approvals, runs, "
    "messages, conversations, refresh_tokens, system_settings, user_settings, users, audit_logs"
)


@pytest.fixture
async def db() -> AsyncIterator[Any]:
    """建表并把一个会话交给用例；用例可直接把它传给 service 层。

    同时承担整库清理：用例结束后关闭会话、TRUNCATE 所有业务表并清理测试存储目录。
    """
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("需要可用的 PostgreSQL：把 TEST_DATABASE_URL 指向本机已有数据库")
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        yield session
    async with get_engine().begin() as conn:
        await conn.execute(text(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE"))
    reset_memory_checkpointer()
    limiter.reset()
    clear_test_transports()
    from app.modules.a2a.client import clear_test_transports as clear_a2a_transports

    clear_a2a_transports()
    await registry.shutdown()
    shutil.rmtree(Path("data/test-attachments"), ignore_errors=True)
    shutil.rmtree(Path("data/test-knowledge"), ignore_errors=True)


# --------------------------------------------------------------- M4 知识库夹具
async def _create_user(*, role: str, display_name: str) -> User:
    """直接写库创建用户并提交，避免测试夹具依赖 HTTP 注册流程。"""
    async with get_session_factory()() as session:
        row = User(
            email=f"{role}-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("password123"),
            display_name=display_name,
            role=role,
        )
        session.add(row)
        await session.commit()
        return row


@pytest.fixture
async def user(db: Any) -> User:
    """已提交的普通用户；服务层测试直接把它作为 owner。"""
    return await _create_user(role="user", display_name="测试用户")


@pytest.fixture
async def admin(db: Any) -> User:
    """已提交的管理员用户。"""
    return await _create_user(role="admin", display_name="管理员")


@pytest.fixture
def admin_auth(admin: User) -> dict[str, str]:
    """管理员请求头（access token 直接签发，无需走登录接口）。"""
    return auth_headers(create_access_token(admin.id).token)


@pytest.fixture
def fake_storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[LocalFileStorage]:
    """隔离的本地存储，并让 service 层通过 get_storage() 拿到同一个实例。"""
    root = Path("data/test-knowledge")
    shutil.rmtree(root, ignore_errors=True)
    storage = LocalFileStorage(root)
    reset_storage()
    monkeypatch.setattr("app.modules.knowledge.service.get_storage", lambda: storage)
    yield storage
    reset_storage()


@pytest.fixture
def fake_embedding(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeEmbeddingProvider]:
    """确定性假向量 provider，并替换 service 层的取用入口。"""
    provider = FakeEmbeddingProvider(get_settings().embedding_dim)
    reset_embedding_provider()
    monkeypatch.setattr("app.modules.knowledge.service.get_embedding_provider", lambda: provider)
    yield provider
    reset_embedding_provider()


async def _seed_ready_document(
    session: AsyncSession,
    storage: LocalFileStorage,
    provider: FakeEmbeddingProvider,
    *,
    owner: User | None,
    title: str,
    filename: str,
    category: str,
    text: str,
) -> KnowledgeDocument:
    """按 service 的规则造一篇 ready 文档：原文落盘 + 真实切分 + 真实向量。"""
    settings = get_settings()
    raw = text.encode("utf-8")
    document = KnowledgeDocument(
        id=uuid.uuid4(),
        owner_user_id=owner.id if owner is not None else None,
        title=title,
        filename=filename,
        source_uri=filename,
        category=category,
        checksum=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        status="ready",
    )
    document.storage_key = f"knowledge/{document.id}{Path(filename).suffix.lower()}"
    await storage.put(document.storage_key, raw)
    chunks = split_text(
        text,
        size=settings.knowledge_chunk_size,
        overlap=settings.knowledge_chunk_overlap,
        max_chunks=settings.knowledge_max_chunks_per_document,
    )
    vectors = await provider.embed(
        [
            f"{chunk.heading}\n{chunk.content}" if chunk.heading else chunk.content
            for chunk in chunks
        ]
    )
    for chunk, vector in zip(chunks, vectors, strict=True):
        document.chunks.append(
            KnowledgeChunk(
                chunk_index=chunk.index,
                content=chunk.content,
                embedding=vector,
                token_count=len(chunk.content),
                metadata_={**chunk.metadata, "char_start": chunk.char_start},
            )
        )
    document.chunk_count = len(chunks)
    session.add(document)
    return document


@pytest.fixture
async def seeded_docs(
    user: User, fake_storage: LocalFileStorage, fake_embedding: FakeEmbeddingProvider
) -> dict[str, Any]:
    """三篇 ready 文档：系统文档、本人文档、另一位用户的私有文档。

    返回 ``{"system": ..., "own": ..., "other": ..., "other_user": ..., "documents": [...]}``，
    供可见性、列表、重建等用例复用；原文均已写入 fake_storage，可直接 rebuild。
    """
    async with get_session_factory()() as session:
        other = User(
            email=f"other-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("password123"),
            display_name="其他用户",
        )
        session.add(other)
        await session.flush()
        system = await _seed_ready_document(
            session,
            fake_storage,
            fake_embedding,
            owner=None,
            title="退货政策",
            filename="return-policy.md",
            category="售后",
            text="# 吊牌要求\n\n退货需要保留吊牌与完整包装。\n\n# 退款时效\n\n退款三个工作日内到账。",
        )
        own = await _seed_ready_document(
            session,
            fake_storage,
            fake_embedding,
            owner=user,
            title="发货时效",
            filename="shipping.md",
            category="物流",
            text="# 发货规则\n\n现货商品 24 小时内发货。",
        )
        foreign = await _seed_ready_document(
            session,
            fake_storage,
            fake_embedding,
            owner=other,
            title="他人私有文档",
            filename="private.md",
            category="售后",
            text="# 私有资料\n\n这是另一位用户的私密暗号。",
        )
        await session.commit()
    return {
        "system": system,
        "own": own,
        "other": foreign,
        "other_user": other,
        "documents": [system, own, foreign],
    }


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
