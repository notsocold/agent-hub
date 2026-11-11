"""A2A 任务查询与动作端点（``/api/v1/tasks``）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.schemas import ErrorEnvelopeOut
from app.core.deps import AdminUser, CurrentUser, DbSession
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

# 动作接口的管理员门禁 403：覆盖 include 级通用 403（capability_disabled 口径）。
_ADMIN_ACTION_RESPONSES: dict[int | str, dict] = {
    403: {
        "model": ErrorEnvelopeOut,
        "description": "需要管理员权限（`forbidden`，`details.required_role=\"admin\"`）",
    }
}


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


@router.post(
    "/{task_id}/cancel", response_model=A2aTaskDetail, responses=_ADMIN_ACTION_RESPONSES
)
async def cancel_task(task_id: uuid.UUID, db: DbSession, admin: AdminUser) -> A2aTaskDetail:
    return await service.cancel_task_action(db, admin, task_id)


@router.post(
    "/{task_id}/resume", response_model=TaskResumeResult, responses=_ADMIN_ACTION_RESPONSES
)
async def resume_task(
    task_id: uuid.UUID, payload: TaskResumeRequest, db: DbSession, admin: AdminUser
) -> TaskResumeResult:
    return await service.resume_task_action(db, admin, task_id, payload)


@router.post(
    "/{task_id}/authorize", response_model=A2aTaskDetail, responses=_ADMIN_ACTION_RESPONSES
)
async def authorize_task(
    task_id: uuid.UUID, payload: TaskAuthorizeRequest, db: DbSession, admin: AdminUser
) -> A2aTaskDetail:
    return await service.authorize_task_action(db, admin, task_id, payload)


@router.post(
    "/{task_id}/reject", response_model=A2aTaskDetail, responses=_ADMIN_ACTION_RESPONSES
)
async def reject_task(
    task_id: uuid.UUID, payload: TaskRejectRequest, db: DbSession, admin: AdminUser
) -> A2aTaskDetail:
    return await service.reject_task_action(db, admin, task_id, payload)
