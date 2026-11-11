"""管理台用户管理业务：列表/详情/角色与状态更新。

唯一新增的管理台业务 SQL 都在这里：列表按 ``created_at DESC, id DESC`` 游标分页，
``stats`` 用一次聚合查询按当前筛选条件计数；PATCH 负责自操作与「最后一个活跃
管理员」保护，并在真实变更时写 ``admin.user.update`` 审计。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.cursor import decode_cursor, encode_cursor
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.db.models.user import User
from app.modules.admin.schemas import (
    AdminUserOut,
    AdminUserPage,
    AdminUserPatchRequest,
    AdminUserStats,
)


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符，避免 ``q`` 里的 %/_/\\ 扩大匹配范围。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _to_out(user: User) -> AdminUserOut:
    return AdminUserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


def _build_filters(
    q: str | None, role: str | None, status: str | None
) -> list[Any]:
    """按 q/role/status 组装筛选条件；游标条件单独追加，不进入 stats。"""
    filters: list[Any] = []
    if q:
        pattern = f"%{_escape_like(q)}%"
        filters.append(
            or_(
                User.email.ilike(pattern, escape="\\"),
                User.display_name.ilike(pattern, escape="\\"),
            )
        )
    if role:
        filters.append(User.role == role)
    if status:
        filters.append(User.status == status)
    return filters


async def _user_stats(db: AsyncSession, filters: list[Any]) -> AdminUserStats:
    stmt = select(
        func.count(User.id).label("total"),
        func.count(User.id).filter(User.status == "active").label("active"),
        func.count(User.id).filter(User.status == "disabled").label("disabled"),
        func.count(User.id).filter(User.role == "admin").label("admins"),
    ).where(*filters)
    row = (await db.execute(stmt)).one()
    return AdminUserStats(
        total=int(row.total),
        active=int(row.active),
        disabled=int(row.disabled),
        admins=int(row.admins),
    )


def _cursor_filter(cursor: str | None) -> Any | None:
    """解码 ``{created_at, id}`` 游标；非法/缺字段统一 422。"""
    if not cursor:
        return None
    decoded = decode_cursor(cursor)
    raw_created_at = decoded.get("created_at")
    raw_id = decoded.get("id")
    if raw_created_at is None or raw_id is None:
        raise ValidationFailedError("分页游标无效")
    try:
        last_created_at = datetime.fromisoformat(str(raw_created_at))
        last_id = uuid.UUID(str(raw_id))
    except (ValueError, TypeError) as exc:
        raise ValidationFailedError("分页游标无效") from exc
    return or_(
        User.created_at < last_created_at,
        and_(
            User.created_at == last_created_at,
            User.id < last_id,
        ),
    )


async def list_users(
    db: AsyncSession,
    *,
    q: str | None,
    role: str | None,
    status: str | None,
    limit: int,
    cursor: str | None,
) -> AdminUserPage:
    filters = _build_filters(q, role, status)
    cursor_condition = _cursor_filter(cursor)
    # stats 不计游标：它永远是当前筛选条件下的全量计数。
    stats = await _user_stats(db, filters)
    page_filters = [*filters, cursor_condition] if cursor_condition is not None else filters

    stmt = (
        select(User)
        .where(*page_filters)
        .order_by(User.created_at.desc(), User.id.desc())
        .limit(limit + 1)
    )
    rows = list((await db.execute(stmt)).scalars())
    has_more = len(rows) > limit
    rows = rows[:limit]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor({"created_at": last.created_at, "id": last.id})

    return AdminUserPage(
        items=[_to_out(row) for row in rows],
        next_cursor=next_cursor,
        has_more=has_more,
        stats=stats,
    )


async def get_user(db: AsyncSession, user_id: uuid.UUID) -> AdminUserOut:
    user = await db.get(User, user_id)
    if user is None:
        raise NotFoundError("用户不存在")
    return _to_out(user)


async def update_user(
    db: AsyncSession,
    admin: User,
    user_id: uuid.UUID,
    payload: AdminUserPatchRequest,
) -> AdminUserOut:
    target = await db.get(User, user_id)
    if target is None:
        raise NotFoundError("用户不存在")

    # 只统计实际变化的字段：同值 PATCH 是 no-op，不写审计。
    changed: set[str] = set()
    if payload.role is not None and payload.role != target.role:
        changed.add("role")
    if payload.status is not None and payload.status != target.status:
        changed.add("status")
    if not changed:
        return _to_out(target)

    # 自操作规则优先于最后管理员保护，返回 422 而不是静默成功。
    if target.id == admin.id and "role" in changed:
        raise ValidationFailedError(
            "不能修改自己的角色", code="cannot_change_own_role"
        )
    if target.id == admin.id and payload.status == "disabled":
        raise ValidationFailedError("不能停用自己的账号", code="cannot_disable_self")

    # 把活跃管理员降级或停用前，先按 id ASC 锁住全部活跃管理员行：固定加锁顺序
    # 防死锁，锁后 refresh 目标再计算剩余数量，防两个管理员并发互相降级。
    demote_or_disable_active_admin = (
        target.role == "admin"
        and target.status == "active"
        and (payload.role == "user" or payload.status == "disabled")
    )
    if demote_or_disable_active_admin:
        locked_ids = list(
            (
                await db.execute(
                    select(User.id)
                    .where(User.role == "admin", User.status == "active")
                    .order_by(User.id)
                    .with_for_update()
                )
            ).scalars()
        )
        await db.refresh(target)
        remaining = len(locked_ids) - (1 if target.id in locked_ids else 0)
        if target.role == "admin" and target.status == "active" and remaining == 0:
            raise ConflictError(
                "不能移除最后一个活跃管理员",
                code="last_admin_protected",
                details={"active_admins": 1},
            )

    if payload.role is not None:
        target.role = payload.role
    if payload.status is not None:
        target.status = payload.status

    await write_audit(
        db,
        actor_type="user",
        actor_id=str(admin.id),
        action="admin.user.update",
        resource_type="user",
        resource_id=str(target.id),
        detail={
            "fields": sorted(changed),
            "role": target.role,
            "status": target.status,
        },
    )
    await db.commit()
    await db.refresh(target)
    return _to_out(target)


__all__ = ["get_user", "list_users", "update_user"]
