"""长期记忆路由。"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Query

from app.core.deps import CurrentUser, DbSession
from app.core.errors import ValidationFailedError
from app.modules.memories import service
from app.modules.memories.schemas import (
    MemoryCreateRequest,
    MemoryExportOut,
    MemoryOut,
    MemoryPage,
    MemoryUpdateRequest,
)

router = APIRouter(prefix="/memories", tags=["记忆"])


@router.get("", response_model=MemoryPage)
async def list_memories(
    user: CurrentUser,
    db: DbSession,
    kind: Literal["profile", "preference", "fact"] | None = None,
    status: Literal["active", "archived"] | None = None,
    q: str | None = Query(default=None, min_length=1, max_length=100),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
) -> MemoryPage:
    return await service.list_memories(
        db, user, kind=kind, status=status, q=q, limit=limit, cursor=cursor
    )


@router.post("", response_model=MemoryOut, status_code=201)
async def create_memory(
    payload: MemoryCreateRequest, user: CurrentUser, db: DbSession
) -> MemoryOut:
    return await service.create_memory(db, user, payload, source={"created_by": "user"})


@router.get("/export", response_model=MemoryExportOut)
async def export_memories(user: CurrentUser, db: DbSession) -> MemoryExportOut:
    return await service.export_memories(db, user)


@router.delete("", status_code=204)
async def clear_memories(
    user: CurrentUser, db: DbSession, confirm: bool = Query(default=False)
) -> None:
    if not confirm:
        raise ValidationFailedError("清空全部记忆需要显式确认：?confirm=true")
    await service.clear_memories(db, user)


@router.get("/{memory_id}", response_model=MemoryOut)
async def get_memory(memory_id: uuid.UUID, user: CurrentUser, db: DbSession) -> MemoryOut:
    return await service.get_memory(db, user, memory_id)


@router.patch("/{memory_id}", response_model=MemoryOut)
async def update_memory(
    memory_id: uuid.UUID,
    payload: MemoryUpdateRequest,
    user: CurrentUser,
    db: DbSession,
) -> MemoryOut:
    return await service.update_memory(db, user, memory_id, payload)


@router.delete("/{memory_id}", status_code=204)
async def delete_memory(memory_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    await service.delete_memory(db, user, memory_id)
