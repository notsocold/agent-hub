"""会话与消息路由。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentUser, DbSession
from app.modules.conversations import service
from app.modules.conversations.schemas import (
    ConversationCreateRequest,
    ConversationDetailOut,
    ConversationOut,
    ConversationPage,
    ConversationUpdateRequest,
    MessagePage,
)

router = APIRouter(prefix="/conversations", tags=["会话"])


@router.get("", response_model=ConversationPage)
async def list_conversations(
    user: CurrentUser,
    db: DbSession,
    q: str | None = Query(default=None, min_length=1, max_length=100),
    status_filter: Literal["active", "archived", "all"] = Query(default="all", alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
) -> ConversationPage:
    return await service.list_conversations(
        db, user, q=q, status=status_filter, limit=limit, cursor=cursor
    )


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: ConversationCreateRequest, user: CurrentUser, db: DbSession
) -> ConversationOut:
    return await service.create_conversation(db, user, payload)


@router.get("/{conversation_id}", response_model=ConversationDetailOut)
async def get_conversation(
    conversation_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> ConversationDetailOut:
    return await service.get_conversation_detail(db, user, conversation_id)


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def update_conversation(
    conversation_id: uuid.UUID,
    payload: ConversationUpdateRequest,
    user: CurrentUser,
    db: DbSession,
) -> ConversationOut:
    return await service.update_conversation(db, user, conversation_id, payload)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(conversation_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    await service.delete_conversation(db, user, conversation_id)


@router.get("/{conversation_id}/messages", response_model=MessagePage)
async def list_messages(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    before: datetime | None = None,
) -> MessagePage:
    return await service.list_messages(
        db, user, conversation_id, limit=limit, cursor=cursor, before=before
    )
