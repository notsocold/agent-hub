"""A2A 任务查询与动作端点（``/api/v1/tasks``）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import CurrentUser, DbSession
from app.modules.tasks import service
from app.modules.tasks.schemas import (
    A2aTaskDetail,
    TaskAuthorizeRequest,
    TaskPage,
    TaskRejectRequest,
    TaskResumeRequest,
    TaskResumeResult,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=TaskPage)
async def list_tasks(
    db: DbSession,
    user: CurrentUser,
    state: Annotated[list[str] | None, Query()] = None,
    agent_id: Annotated[uuid.UUID | None, Query()] = None,
    run_id: Annotated[uuid.UUID | None, Query()] = None,
    conversation_id: Annotated[uuid.UUID | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
) -> TaskPage:
    del user
    return await service.list_tasks(
        db,
        states=state or None,
        agent_id=agent_id,
        run_id=run_id,
        conversation_id=conversation_id,
        q=q,
        from_=from_,
        to=to,
        limit=limit,
        cursor=cursor,
    )


@router.get("/{task_id}", response_model=A2aTaskDetail)
async def get_task(task_id: uuid.UUID, db: DbSession, user: CurrentUser) -> A2aTaskDetail:
    del user
    return await service.get_task_detail(db, task_id)


@router.post("/{task_id}/cancel", response_model=A2aTaskDetail)
async def cancel_task(task_id: uuid.UUID, db: DbSession, user: CurrentUser) -> A2aTaskDetail:
    return await service.cancel_task_action(db, user, task_id)


@router.post("/{task_id}/resume", response_model=TaskResumeResult)
async def resume_task(
    task_id: uuid.UUID, payload: TaskResumeRequest, db: DbSession, user: CurrentUser
) -> TaskResumeResult:
    return await service.resume_task_action(db, user, task_id, payload)


@router.post("/{task_id}/authorize", response_model=A2aTaskDetail)
async def authorize_task(
    task_id: uuid.UUID, payload: TaskAuthorizeRequest, db: DbSession, user: CurrentUser
) -> A2aTaskDetail:
    return await service.authorize_task_action(db, user, task_id, payload)


@router.post("/{task_id}/reject", response_model=A2aTaskDetail)
async def reject_task(
    task_id: uuid.UUID, payload: TaskRejectRequest, db: DbSession, user: CurrentUser
) -> A2aTaskDetail:
    return await service.reject_task_action(db, user, task_id, payload)
