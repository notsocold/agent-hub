"""会话与消息业务。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cursor import decode_cursor, encode_cursor
from app.core.errors import NotFoundError, ValidationFailedError
from app.db.models.approval import Approval
from app.db.models.chat import Run
from app.db.models.conversation import Conversation, Message
from app.db.models.user import User
from app.modules.conversations.schemas import (
    CitationOut,
    ConversationCreateRequest,
    ConversationDetailOut,
    ConversationOut,
    ConversationPage,
    ConversationUpdateRequest,
    LastRunOut,
    MessageArtifactOut,
    MessageAttachmentOut,
    MessageCapabilitiesOut,
    MessageErrorOut,
    MessageOut,
    MessagePage,
    UsageOut,
)

_DELETED = "deleted"


def _now() -> datetime:
    return datetime.now(UTC)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _pending_approval_subquery():
    now = _now()
    return (
        select(func.count(Approval.id))
        .join(Run, Approval.run_id == Run.id)
        .where(
            Run.conversation_id == Conversation.id,
            Approval.status == "pending",
            or_(Approval.expires_at.is_(None), Approval.expires_at > now),
        )
        .correlate(Conversation)
        .scalar_subquery()
    )


def _conversation_out(row: Any) -> ConversationOut:
    conversation: Conversation = row[0]
    pending = int(row[1] or 0)
    return ConversationOut(
        id=conversation.id,
        title=conversation.title,
        status="archived" if conversation.status == "archived" else "active",
        message_count=conversation.message_count,
        last_message_preview=conversation.last_message_preview,
        last_message_at=conversation.last_message_at,
        pending_approval_count=pending,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def _apply_cursor(stmt, cursor: dict[str, Any]):
    if not cursor:
        return stmt
    raw_time = cursor.get("t")
    raw_id = cursor.get("id")
    try:
        last_time = datetime.fromisoformat(raw_time) if raw_time else None
        last_id = uuid.UUID(str(raw_id)) if raw_id else None
    except (ValueError, TypeError) as exc:
        raise ValidationFailedError("分页游标无效") from exc
    if last_id is None:
        raise ValidationFailedError("分页游标无效")
    if last_time is None:
        return stmt.where(
            Conversation.last_message_at.is_(None),
            Conversation.id < last_id,
        )
    return stmt.where(
        or_(
            Conversation.last_message_at < last_time,
            and_(Conversation.last_message_at == last_time, Conversation.id < last_id),
        )
    )


async def list_conversations(
    db: AsyncSession,
    user: User,
    *,
    q: str | None,
    status: str,
    limit: int,
    cursor: str | None,
) -> ConversationPage:
    pending = _pending_approval_subquery()
    base = select(Conversation, pending.label("pending_count")).where(
        Conversation.user_id == user.id,
        Conversation.status != _DELETED,
    )
    if q:
        base = base.where(Conversation.title.ilike(f"%{_escape_like(q)}%", escape="\\"))
    if status in {"active", "archived"}:
        base = base.where(Conversation.status == status)

    stmt = base.order_by(
        Conversation.last_message_at.desc().nullslast(), Conversation.id.desc()
    ).limit(limit + 1)
    stmt = _apply_cursor(stmt, decode_cursor(cursor))
    rows = (await db.execute(stmt)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_conversation_out(row) for row in rows]
    next_cursor = None
    if has_more and rows:
        last = rows[-1][0]
        next_cursor = encode_cursor(
            {"t": last.last_message_at.isoformat() if last.last_message_at else None, "id": last.id}
        )

    stats_stmt = select(
        func.count(Conversation.id).label("total"),
        func.count(Conversation.id).filter(Conversation.status == "active").label("active"),
        func.count(Conversation.id).filter(Conversation.status == "archived").label("archived"),
    ).where(Conversation.user_id == user.id, Conversation.status != _DELETED)
    if q:
        stats_stmt = stats_stmt.where(Conversation.title.ilike(f"%{_escape_like(q)}%", escape="\\"))
    if status in {"active", "archived"}:
        stats_stmt = stats_stmt.where(Conversation.status == status)
    stats_row = (await db.execute(stats_stmt)).one()
    pending_total = await db.scalar(
        select(func.count(func.distinct(Approval.id)))
        .join(Run, Approval.run_id == Run.id)
        .join(Conversation, Run.conversation_id == Conversation.id)
        .where(
            Conversation.user_id == user.id,
            Conversation.status != _DELETED,
            Approval.status == "pending",
            or_(Approval.expires_at.is_(None), Approval.expires_at > _now()),
        )
    )
    return ConversationPage(
        items=items,
        next_cursor=next_cursor,
        has_more=has_more,
        stats={
            "total": int(stats_row.total),
            "active": int(stats_row.active),
            "archived": int(stats_row.archived),
            "pending_approval": int(pending_total or 0),
        },
    )


async def get_owned_conversation(
    db: AsyncSession, user: User, conversation_id: uuid.UUID
) -> Conversation:
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != user.id or conversation.status == _DELETED:
        raise NotFoundError("会话不存在")
    return conversation


async def get_conversation_detail(
    db: AsyncSession, user: User, conversation_id: uuid.UUID
) -> ConversationDetailOut:
    conversation = await get_owned_conversation(db, user, conversation_id)
    pending = await db.scalar(
        select(func.count(Approval.id))
        .join(Run, Approval.run_id == Run.id)
        .where(
            Run.conversation_id == conversation.id,
            Approval.status == "pending",
            or_(Approval.expires_at.is_(None), Approval.expires_at > _now()),
        )
    )
    last_run = await db.scalar(
        select(Run)
        .where(Run.conversation_id == conversation.id)
        .order_by(Run.created_at.desc())
        .limit(1)
    )
    base = ConversationOut(
        id=conversation.id,
        title=conversation.title,
        status="archived" if conversation.status == "archived" else "active",
        message_count=conversation.message_count,
        last_message_preview=conversation.last_message_preview,
        last_message_at=conversation.last_message_at,
        pending_approval_count=int(pending or 0),
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )
    return ConversationDetailOut(
        **base.model_dump(),
        archived_at=conversation.archived_at,
        last_run=(
            LastRunOut(
                id=last_run.id,
                status=last_run.status,
                model=last_run.model or None,
                finished_at=last_run.finished_at,
            )
            if last_run
            else None
        ),
    )


async def create_conversation(
    db: AsyncSession, user: User, payload: ConversationCreateRequest
) -> ConversationOut:
    conversation = Conversation(
        user_id=user.id,
        title=payload.title or "新对话",
    )
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return _conversation_out((conversation, 0))


async def update_conversation(
    db: AsyncSession,
    user: User,
    conversation_id: uuid.UUID,
    payload: ConversationUpdateRequest,
) -> ConversationOut:
    conversation = await get_owned_conversation(db, user, conversation_id)
    if payload.title is not None:
        conversation.title = payload.title
    if payload.status is not None:
        conversation.status = payload.status
        conversation.archived_at = _now() if payload.status == "archived" else None
    await db.commit()
    await db.refresh(conversation)
    return _conversation_out((conversation, 0))


async def delete_conversation(db: AsyncSession, user: User, conversation_id: uuid.UUID) -> None:
    conversation = await get_owned_conversation(db, user, conversation_id)
    conversation.status = _DELETED
    conversation.archived_at = _now()
    await db.commit()


def _normalize_capabilities(raw: dict[str, Any] | None) -> MessageCapabilitiesOut:
    data = dict(raw or {})
    return MessageCapabilitiesOut(
        skills=[str(item) for item in data.get("skills", [])],
        mcp_servers=[str(item) for item in data.get("mcp_servers", [])],
        mcp_tools=[str(item) for item in data.get("mcp_tools", [])],
        knowledge_categories=[str(item) for item in data.get("knowledge_categories", [])],
        workflow_id=data.get("workflow_id"),
    )


def _message_out(message: Message) -> MessageOut:
    status = message.status if message.status in {"completed", "failed", "canceled"} else "canceled"
    usage = UsageOut(**message.usage) if message.usage else None
    error = MessageErrorOut(**message.error) if message.error else None
    return MessageOut(
        id=message.id,
        role=message.role,  # type: ignore[arg-type]
        content=message.content,
        content_type=message.content_type,
        run_id=message.run_id,
        client_message_id=message.client_message_id,
        status=status,  # type: ignore[arg-type]
        citations=[CitationOut(**item) for item in (message.citations or [])],
        attachments=[MessageAttachmentOut(**item) for item in (message.attachments or [])],
        capabilities=_normalize_capabilities(message.capabilities),
        artifacts=[MessageArtifactOut(**item) for item in (message.artifacts or [])],
        usage=usage,
        error=error,
        created_at=message.created_at,
        metadata=message.metadata_ or {},
    )


def _apply_message_cursor(stmt, cursor: dict[str, Any]):
    if not cursor:
        return stmt
    raw_time = cursor.get("t")
    raw_id = cursor.get("id")
    try:
        last_time = datetime.fromisoformat(raw_time) if raw_time else None
        last_id = uuid.UUID(str(raw_id)) if raw_id else None
    except (ValueError, TypeError) as exc:
        raise ValidationFailedError("分页游标无效") from exc
    if last_time is None or last_id is None:
        raise ValidationFailedError("分页游标无效")
    return stmt.where(
        or_(
            Message.created_at < last_time,
            and_(Message.created_at == last_time, Message.id < last_id),
        )
    )


async def list_messages(
    db: AsyncSession,
    user: User,
    conversation_id: uuid.UUID,
    *,
    limit: int,
    cursor: str | None,
    before: datetime | None,
) -> MessagePage:
    await get_owned_conversation(db, user, conversation_id)
    stmt = (
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            # 生成中的助手消息不进入历史，避免前端重连时看到空的“已取消”消息；
            # 运行结束后由 finalize 落成 completed/failed/canceled。
            Message.status != "streaming",
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit + 1)
    )
    if before is not None:
        stmt = stmt.where(Message.created_at < before)
    stmt = _apply_message_cursor(stmt, decode_cursor(cursor))
    rows = list((await db.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor({"t": last.created_at.isoformat(), "id": last.id})
    return MessagePage(
        items=[_message_out(item) for item in rows],
        next_cursor=next_cursor,
        has_more=has_more,
    )
