"""管理台只读概览：空库基线、全表计数口径、查询上界与访问控制。

覆盖 M8 T2 契约：

- ``users``/``conversations``/``messages``/``runs``/``a2a_tasks``/``knowledge`` 为
  全表原始计数（含软删/失败/终态），``conversations.deleted`` 单列；
- ``attachments.total`` 只计 ``status != 'deleted'`` 的行，``storage_bytes`` 为这些
  行的 ``SUM(size_bytes)``，``deleted`` 单独计数；
- 所有状态键始终存在，空库时为 ``0``；``generated_at`` 是 UTC 时间；
- 一次请求的 SELECT 数有上界（鉴权 1 + 8 条聚合 + 会话开销，≤10）；
- 普通用户 403 ``forbidden`` + ``details.required_role="admin"``，无 token 401。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import httpx
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import create_access_token, hash_password
from app.db.models.attachment import Attachment
from app.db.models.chat import Run
from app.db.models.conversation import Conversation, Message
from app.db.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.db.models.remote import A2aTask, RemoteAgent
from app.db.models.user import User
from app.db.session import get_engine
from tests.conftest import auth_headers

_TOP_LEVEL_FIELDS = {
    "generated_at",
    "users",
    "conversations",
    "messages",
    "runs",
    "a2a_tasks",
    "knowledge",
    "attachments",
}


async def _create_disabled_user(db: AsyncSession) -> User:
    row = User(
        email=f"disabled-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password("password123"),
        display_name="停用用户",
        status="disabled",
    )
    db.add(row)
    await db.flush()
    return row


async def _seed_overview_rows(db: AsyncSession, user: User) -> None:
    """直接落库构造 brief 约定的 3/4/5/6/2/2/3/3 数据集。"""
    await _create_disabled_user(db)

    # 4 个会话：active/active/archived/deleted；deleted 只影响 conversations 自身计数，
    # 其下消息与 run 仍按全表原始行计数。
    conversations = [
        Conversation(user_id=user.id, title="活跃会话一", status="active"),
        Conversation(user_id=user.id, title="活跃会话二", status="active"),
        Conversation(user_id=user.id, title="归档会话", status="archived"),
        Conversation(user_id=user.id, title="已删会话", status="deleted"),
    ]
    db.add_all(conversations)
    await db.flush()
    first, second, archived, deleted = conversations

    # 5 条消息：1 个会话 2 条，其余各 1 条。
    messages = [
        Message(conversation_id=first.id, role="user", content="你好"),
        Message(conversation_id=first.id, role="assistant", content="你好，有什么可以帮你"),
        Message(conversation_id=second.id, role="user", content="再来一次"),
        Message(conversation_id=archived.id, role="user", content="归档旧消息"),
        Message(conversation_id=deleted.id, role="user", content="软删会话里的消息"),
    ]
    db.add_all(messages)
    await db.flush()

    # 6 个 run：三个活跃状态各占一个不同会话；三个终态复用 deleted 会话。
    runs = [
        Run(conversation_id=first.id, status="queued", model="fake"),
        Run(conversation_id=second.id, status="running", model="fake"),
        Run(conversation_id=archived.id, status="interrupted", model="fake"),
        Run(conversation_id=deleted.id, status="succeeded", model="fake"),
        Run(conversation_id=deleted.id, status="failed", model="fake"),
        Run(conversation_id=deleted.id, status="canceled", model="fake"),
    ]
    db.add_all(runs)
    await db.flush()

    # 1 个 RemoteAgent + 2 个 A2aTask（completed/failed）。
    agent = RemoteAgent(
        slug=f"overview-{uuid.uuid4().hex[:8]}",
        name="概览测试 Agent",
        url="http://a2a.test/agent",
    )
    db.add(agent)
    await db.flush()
    db.add_all(
        [
            A2aTask(
                a2a_task_id=f"task-{uuid.uuid4().hex[:8]}",
                remote_agent_id=agent.id,
                state="completed",
            ),
            A2aTask(
                a2a_task_id=f"task-{uuid.uuid4().hex[:8]}",
                remote_agent_id=agent.id,
                state="failed",
            ),
        ]
    )
    await db.flush()

    # 2 篇知识库文档（ready/failed），ready 文档下 3 个 chunk。
    ready_document = KnowledgeDocument(
        title="已就绪文档",
        checksum=uuid.uuid4().hex,
        status="ready",
    )
    failed_document = KnowledgeDocument(
        title="索引失败文档",
        checksum=uuid.uuid4().hex,
        status="failed",
    )
    db.add_all([ready_document, failed_document])
    await db.flush()
    embedding = [0.0] * get_settings().embedding_dim
    db.add_all(
        [
            KnowledgeChunk(
                document_id=ready_document.id,
                chunk_index=index,
                content=f"片段 {index}",
                embedding=embedding,
            )
            for index in range(3)
        ]
    )

    # 3 个附件：ready 100、processing 200、deleted 999；前两者计入 total/字节数。
    db.add_all(
        [
            Attachment(
                user_id=user.id,
                filename="ready.txt",
                checksum=uuid.uuid4().hex,
                status="ready",
                size_bytes=100,
            ),
            Attachment(
                user_id=user.id,
                filename="processing.txt",
                checksum=uuid.uuid4().hex,
                status="processing",
                size_bytes=200,
            ),
            Attachment(
                user_id=user.id,
                filename="deleted.txt",
                checksum=uuid.uuid4().hex,
                status="deleted",
                size_bytes=999,
            ),
        ]
    )
    await db.commit()


async def test_overview_empty_counts_are_zero(
    client: httpx.AsyncClient,
    admin_auth: dict[str, str],
) -> None:
    """空库基线：只有 admin_auth 创建的管理员，其余所有叶子字段为 0。"""
    response = await client.get("/api/v1/admin/overview", headers=admin_auth)
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == _TOP_LEVEL_FIELDS
    assert body["users"] == {"total": 1, "active": 1, "disabled": 0, "admins": 1}
    assert body["conversations"] == {
        "total": 0,
        "active": 0,
        "archived": 0,
        "deleted": 0,
    }
    assert body["messages"] == {"total": 0}
    assert body["runs"] == {
        "total": 0,
        "queued": 0,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
        "canceled": 0,
        "interrupted": 0,
    }
    assert body["a2a_tasks"] == {
        "total": 0,
        "submitted": 0,
        "working": 0,
        "input_required": 0,
        "auth_required": 0,
        "completed": 0,
        "failed": 0,
        "canceled": 0,
        "rejected": 0,
    }
    assert body["knowledge"] == {
        "documents": 0,
        "chunks": 0,
        "documents_pending": 0,
        "documents_indexing": 0,
        "documents_ready": 0,
        "documents_failed": 0,
    }
    assert body["attachments"] == {"total": 0, "storage_bytes": 0, "deleted": 0}

    generated_at = datetime.fromisoformat(body["generated_at"])
    assert generated_at.tzinfo is not None
    assert generated_at.utcoffset() == timedelta(0)


async def test_overview_counts_match_seeded_rows(
    client: httpx.AsyncClient,
    admin_auth: dict[str, str],
    user: User,
    db: AsyncSession,
) -> None:
    """有数据时逐项断言 brief 的目标数字，包含软删/失败/终态行。"""
    await _seed_overview_rows(db, user)

    response = await client.get("/api/v1/admin/overview", headers=admin_auth)
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == _TOP_LEVEL_FIELDS
    assert body["users"] == {"total": 3, "active": 2, "disabled": 1, "admins": 1}
    assert body["conversations"] == {
        "total": 4,
        "active": 2,
        "archived": 1,
        "deleted": 1,
    }
    assert body["messages"] == {"total": 5}
    assert body["runs"] == {
        "total": 6,
        "queued": 1,
        "running": 1,
        "succeeded": 1,
        "failed": 1,
        "canceled": 1,
        "interrupted": 1,
    }
    assert body["a2a_tasks"] == {
        "total": 2,
        "submitted": 0,
        "working": 0,
        "input_required": 0,
        "auth_required": 0,
        "completed": 1,
        "failed": 1,
        "canceled": 0,
        "rejected": 0,
    }
    assert body["knowledge"] == {
        "documents": 2,
        "chunks": 3,
        "documents_pending": 0,
        "documents_indexing": 0,
        "documents_ready": 1,
        "documents_failed": 1,
    }
    assert body["attachments"] == {"total": 2, "storage_bytes": 300, "deleted": 1}

    generated_at = datetime.fromisoformat(body["generated_at"])
    assert generated_at.tzinfo is not None
    assert generated_at.utcoffset() == timedelta(0)


async def test_overview_uses_bounded_queries(
    client: httpx.AsyncClient,
    admin_auth: dict[str, str],
) -> None:
    """一次请求最多 10 条 SELECT（鉴权 + 8 条聚合 + 会话开销）。"""
    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sync_engine = get_engine().sync_engine
    event.listen(sync_engine, "before_cursor_execute", _capture)
    try:
        response = await client.get("/api/v1/admin/overview", headers=admin_auth)
    finally:
        event.remove(sync_engine, "before_cursor_execute", _capture)

    assert response.status_code == 200, response.text
    selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 10, selects


async def test_overview_requires_admin(
    client: httpx.AsyncClient,
    user: User,
) -> None:
    """普通用户 403 forbidden + required_role；无 token 401。"""
    headers = auth_headers(create_access_token(user.id).token)

    forbidden = await client.get("/api/v1/admin/overview", headers=headers)
    assert forbidden.status_code == 403, forbidden.text
    error = forbidden.json()["error"]
    assert error["code"] == "forbidden"
    assert error["message"] == "需要管理员权限"
    assert error["details"] == {"required_role": "admin"}

    unauthorized = await client.get("/api/v1/admin/overview")
    assert unauthorized.status_code == 401, unauthorized.text
    assert unauthorized.json()["error"]["code"] == "unauthorized"


async def test_overview_does_not_write_audit(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """只读概览成功请求不写审计（连续两次也不产生记录）。"""
    before = await client.get(
        "/api/v1/admin/audit-logs", params={"limit": 100}, headers=admin_auth
    )
    assert before.status_code == 200, before.text
    assert before.json()["items"] == []

    for _ in range(2):
        response = await client.get("/api/v1/admin/overview", headers=admin_auth)
        assert response.status_code == 200, response.text

    after = await client.get(
        "/api/v1/admin/audit-logs", params={"limit": 100}, headers=admin_auth
    )
    assert after.status_code == 200, after.text
    assert after.json()["items"] == []
