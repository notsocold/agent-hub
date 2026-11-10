"""审计日志检索。

只读接口：普通用户固定看自己的 ``actor_type=user`` 记录；管理员可全局按
actor/action/资源/时间过滤。分页与既有资源一致：``(created_at, id)`` 倒序 + 不透明游标。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cursor import decode_cursor, encode_cursor
from app.core.errors import ValidationFailedError
from app.db.models.audit import AuditLog
from app.modules.audit.schemas import AuditLogOut, AuditLogPage


def _to_out(row: AuditLog) -> AuditLogOut:
    return AuditLogOut(
        id=row.id,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        detail=row.detail or {},
        request_id=row.request_id,
        created_at=row.created_at,
    )


async def list_audit_logs(
    db: AsyncSession,
    *,
    actor_type: str | None = None,
    actor_id: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> AuditLogPage:
    stmt = select(AuditLog)
    if actor_type:
        stmt = stmt.where(AuditLog.actor_type == actor_type)
    if actor_id:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    if resource_id:
        stmt = stmt.where(AuditLog.resource_id == resource_id)
    if start is not None:
        stmt = stmt.where(AuditLog.created_at >= start)
    if end is not None:
        stmt = stmt.where(AuditLog.created_at < end)

    cursor_data = decode_cursor(cursor)
    if cursor_data:
        raw_time = cursor_data.get("t")
        raw_id = cursor_data.get("id")
        try:
            last_time = datetime.fromisoformat(str(raw_time)) if raw_time else None
            last_id = int(raw_id) if raw_id is not None else None
        except (ValueError, TypeError) as exc:
            raise ValidationFailedError("分页游标无效") from exc
        if last_time is None or last_id is None:
            raise ValidationFailedError("分页游标无效")
        stmt = stmt.where(
            or_(
                AuditLog.created_at < last_time,
                and_(AuditLog.created_at == last_time, AuditLog.id < last_id),
            )
        )

    stmt = stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit + 1)
    rows = list((await db.execute(stmt)).scalars())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor({"t": last.created_at.isoformat(), "id": last.id})
    return AuditLogPage(
        items=[_to_out(row) for row in rows], next_cursor=next_cursor, has_more=has_more
    )


__all__ = ["list_audit_logs"]
