"""管理台路由：用户列表/详情/角色与状态更新，以及只读概览。

本文件是 M8 管理台路由骨架（``prefix="/admin"``）；后续任务在同一文件追加
平台指令、系统技能/MCP、全局 Agent 端点。鉴权统一走 M7 的 ``AdminUser``，
403 信封由 ``require_admin`` 产生，不在这里重复判断角色。
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Query

from app.core.deps import AdminUser, DbSession
from app.modules.admin import service
from app.modules.admin.schemas import (
    AdminOverviewOut,
    AdminUserOut,
    AdminUserPage,
    AdminUserPatchRequest,
)

router = APIRouter(prefix="/admin", tags=["管理台"])


@router.get(
    "/users",
    response_model=AdminUserPage,
    summary="管理员分页查询用户",
    description=(
        "按创建时间倒序返回用户。`q` 模糊匹配邮箱或展示名（大小写不敏感），"
        "`role`/`status` 为枚举过滤；`stats` 是当前筛选条件下的全量计数。"
    ),
)
async def list_users(
    admin: AdminUser,
    db: DbSession,
    q: str | None = Query(default=None, max_length=100, description="邮箱或展示名片段"),
    role: Literal["user", "admin"] | None = Query(default=None, description="角色过滤"),
    status_filter: Literal["active", "disabled"] | None = Query(
        default=None, alias="status", description="状态过滤"
    ),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=500),
) -> AdminUserPage:
    return await service.list_users(
        db, q=q, role=role, status=status_filter, limit=limit, cursor=cursor
    )


@router.get(
    "/users/{user_id}",
    response_model=AdminUserOut,
    summary="管理员查看用户详情",
    description="返回与列表项相同形状的用户字段；响应永不包含密码散列。",
)
async def get_user(
    user_id: uuid.UUID, admin: AdminUser, db: DbSession
) -> AdminUserOut:
    return await service.get_user(db, user_id)


@router.patch(
    "/users/{user_id}",
    response_model=AdminUserOut,
    summary="管理员修改用户角色或状态",
    description=(
        "只允许修改 `role`/`status`，且至少提交一项；不能修改自己的角色、"
        "也不能停用自己；移除最后一个活跃管理员返回 409 `last_admin_protected`。"
        "真实变更写 `admin.user.update` 审计，同值 PATCH 直接返回当前值。"
    ),
)
async def update_user(
    user_id: uuid.UUID,
    payload: AdminUserPatchRequest,
    admin: AdminUser,
    db: DbSession,
) -> AdminUserOut:
    return await service.update_user(db, admin, user_id, payload)


@router.get(
    "/overview",
    response_model=AdminOverviewOut,
    summary="管理员查看只读概览",
    description=(
        "实时返回用户、会话、消息、运行、A2A 任务、知识库与附件的全表计数。"
        "``users``/``conversations``/``messages``/``runs``/``a2a_tasks``/``knowledge`` "
        "含软删/失败/终态行；``conversations.deleted`` 单列；``attachments`` 只计"
        "未删除行，``deleted`` 单独计数。所有状态键始终存在，无数据时为 0。"
        "该接口只读、不写审计，每次请求执行 8 条聚合查询。"
    ),
)
async def get_overview(admin: AdminUser, db: DbSession) -> AdminOverviewOut:
    return await service.build_overview(db)
