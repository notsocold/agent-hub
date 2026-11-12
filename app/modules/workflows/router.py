"""M9 工作流路由：步骤模板 CRUD/启停与运行历史查询。

越权口径与 service 对齐：他人 user 模板与不存在同样 404（不泄露存在性）；
系统模板人人可读、任何人不可改删；runs 仅模板 owner 可查（系统模板 403）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from app.core.deps import CurrentUser, DbSession
from app.modules.workflows import service
from app.modules.workflows.schemas import (
    WorkflowCreate,
    WorkflowEnabledRequest,
    WorkflowListOut,
    WorkflowOut,
    WorkflowRunListOut,
    WorkflowUpdate,
)

router = APIRouter(prefix="/workflows", tags=["工作流"])


@router.post("", response_model=WorkflowOut, status_code=201)
async def create_workflow(
    payload: WorkflowCreate, user: CurrentUser, db: DbSession
) -> WorkflowOut:
    """创建本人步骤模板（scope=user、version=1、enabled=True）。"""
    return await service.create_workflow(
        db, user, name=payload.name, description=payload.description, steps=payload.steps
    )


@router.get("", response_model=WorkflowListOut)
async def list_workflows(
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=500),
) -> WorkflowListOut:
    """系统模板 + 本人模板，按 ``created_at DESC`` 游标分页。"""
    return await service.list_workflows(db, user, limit=limit, cursor=cursor)


@router.get("/{workflow_id}", response_model=WorkflowOut)
async def get_workflow(
    workflow_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> WorkflowOut:
    """详情：系统或本人 → 200；他人/不存在 → 404。"""
    return await service.get_workflow(db, user, workflow_id)


@router.patch("/{workflow_id}", response_model=WorkflowOut)
async def update_workflow(
    workflow_id: uuid.UUID,
    payload: WorkflowUpdate,
    user: CurrentUser,
    db: DbSession,
) -> WorkflowOut:
    """PATCH 本人模板（系统/他人一律 404）：成功 ``version += 1``。"""
    return await service.update_workflow(
        db,
        user,
        workflow_id,
        name=payload.name,
        description=payload.description,
        steps=payload.steps,
    )


@router.put("/{workflow_id}/enabled", response_model=WorkflowOut)
async def set_workflow_enabled(
    workflow_id: uuid.UUID,
    payload: WorkflowEnabledRequest,
    user: CurrentUser,
    db: DbSession,
) -> WorkflowOut:
    """启停本人模板：只改 ``enabled`` 位，不改 version。"""
    return await service.set_enabled(db, user, workflow_id, enabled=payload.enabled)


@router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(
    workflow_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> None:
    """硬删本人模板（``workflow_runs`` 随 FK CASCADE）。"""
    await service.delete_workflow(db, user, workflow_id)


@router.get("/{workflow_id}/runs", response_model=WorkflowRunListOut)
async def list_workflow_runs(
    workflow_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=500),
) -> WorkflowRunListOut:
    """运行历史：仅模板 owner 可查；系统模板 403，他人/不存在 404。"""
    return await service.list_workflow_runs(db, user, workflow_id, limit=limit, cursor=cursor)
