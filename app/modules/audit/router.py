"""审计日志路由：用户自查 + 管理员全局检索。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import AdminUser, CurrentUser, DbSession
from app.modules.audit.schemas import AuditLogPage
from app.modules.audit.service import list_audit_logs

router = APIRouter(tags=["审计日志"])


@router.get(
    "/audit-logs",
    response_model=AuditLogPage,
    summary="查询当前用户的审计日志",
    description=(
        "固定返回 `actor_type=user` 且 `actor_id` 为当前用户的记录；"
        "支持 action、resource_type、时间范围与游标分页。"
    ),
)
async def list_own_audit_logs(
    user: CurrentUser,
    db: DbSession,
    action: str | None = Query(default=None, max_length=64),
    resource_type: str | None = Query(default=None, max_length=64),
    start: Annotated[datetime | None, Query(description="起始时间（含）")] = None,
    end: Annotated[datetime | None, Query(description="结束时间（不含）")] = None,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
) -> AuditLogPage:
    return await list_audit_logs(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action=action,
        resource_type=resource_type,
        start=start,
        end=end,
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/admin/audit-logs",
    response_model=AuditLogPage,
    summary="管理员查询全局审计日志",
    description="按 actor、action、资源与时间范围检索全部用户的操作记录。",
)
async def list_all_audit_logs(
    admin: AdminUser,
    db: DbSession,
    actor_type: str | None = Query(default=None, max_length=16),
    actor_id: str | None = Query(default=None, max_length=64),
    action: str | None = Query(default=None, max_length=64),
    resource_type: str | None = Query(default=None, max_length=64),
    resource_id: str | None = Query(default=None, max_length=64),
    start: Annotated[datetime | None, Query(description="起始时间（含）")] = None,
    end: Annotated[datetime | None, Query(description="结束时间（不含）")] = None,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
) -> AuditLogPage:
    return await list_audit_logs(
        db,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        start=start,
        end=end,
        limit=limit,
        cursor=cursor,
    )
