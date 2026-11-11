"""管理台业务：用户列表/详情/角色与状态更新、只读概览与平台指令视图装配。

唯一新增的管理台业务 SQL 都在这里：列表按 ``created_at DESC, id DESC`` 游标分页，
``stats`` 用一次聚合查询按当前筛选条件计数；PATCH 负责自操作与「最后一个活跃
管理员」保护，并在真实变更时写 ``admin.user.update`` 审计。概览按表各执行一条
聚合查询（共 8 条），只取计数与求和，不加载 ORM 行。平台默认指令的校验、写入
与审计留在 ``app.modules.instructions.service``，这里只装配管理端视图。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.cursor import decode_cursor, encode_cursor
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.db.models.attachment import Attachment
from app.db.models.chat import Run
from app.db.models.conversation import Conversation, Message
from app.db.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.db.models.remote import A2A_TASK_STATES, A2aTask
from app.db.models.system import UserSetting
from app.db.models.user import User
from app.modules.admin.schemas import (
    AdminInstructionOut,
    AdminOverviewA2aTasks,
    AdminOverviewAttachments,
    AdminOverviewConversations,
    AdminOverviewKnowledge,
    AdminOverviewMessages,
    AdminOverviewOut,
    AdminOverviewRuns,
    AdminOverviewUsers,
    AdminUserOut,
    AdminUserPage,
    AdminUserPatchRequest,
    AdminUserStats,
)
from app.modules.instructions.service import (
    get_platform_instruction,
    set_platform_instruction,
)

_RUN_STATUSES = ("queued", "running", "succeeded", "failed", "canceled", "interrupted")


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


async def get_admin_instruction(db: AsyncSession) -> AdminInstructionOut:
    """装配平台默认指令视图：文本、来源、上限、个人覆盖计数与最后修改人。"""
    content, row = await get_platform_instruction(db)
    user_override_count = int(
        await db.scalar(
            select(func.count())
            .select_from(UserSetting)
            .where(UserSetting.instructions.is_not(None))
        )
        or 0
    )
    updated_by = row.updated_by if row is not None else None
    updated_by_email = None
    if updated_by is not None:
        updater = await db.get(User, updated_by)
        updated_by_email = updater.email if updater is not None else None
    return AdminInstructionOut(
        content=content,
        is_default=row is None,
        max_chars=get_settings().instruction_max_chars,
        user_override_count=user_override_count,
        updated_by=updated_by,
        updated_by_email=updated_by_email,
        updated_at=row.updated_at if row is not None else None,
    )


async def update_admin_instruction(
    db: AsyncSession, admin: User, content: str | None
) -> AdminInstructionOut:
    """保存/恢复平台默认指令，然后返回与管理端 GET 同形的视图。"""
    await set_platform_instruction(db, admin, content)
    return await get_admin_instruction(db)


def _counts_by_status(rows: Any) -> dict[str, int]:
    """把 ``GROUP BY status`` 的 ``(status, count)`` 行汇总成字典。

    某状态没有数据时字典里没有该键，由调用方按固定状态元组补 ``0``，保证
    响应里的状态键始终存在。
    """
    counts: dict[str, int] = defaultdict(int)
    for status, count in rows:
        counts[status] += int(count or 0)
    return counts


async def build_overview(db: AsyncSession) -> AdminOverviewOut:
    """实时汇总管理台只读概览。

    计数口径：``users``/``conversations``/``messages``/``runs``/``a2a_tasks``/
    ``knowledge`` 为全表原始计数（含软删/失败/终态），``conversations.deleted``
    单列；``attachments.total`` 只计 ``status != 'deleted'`` 的行，
    ``storage_bytes`` 为这些行的 ``COALESCE(SUM(size_bytes), 0)``，``deleted``
    单独计数。所有状态键始终存在，无数据时为 ``0``。

    SQL 口径：每张表最多一条聚合查询，共 8 条（``users`` 一条
    ``GROUP BY status, role``；``conversations``/``runs``/``a2a_tasks``/
    ``knowledge_documents``/``attachments`` 各一条 ``GROUP BY status``；
    ``messages``/``knowledge_chunks`` 各一条 ``COUNT``），不加载 ORM 行、不做 N+1。

    性能口径：精确计数，O(表大小)；管理端低频只读；表规模增大后再评估统计表，
    不在 M8。
    """
    # 1) users：一条 GROUP BY status, role 同时得到 total/active/disabled/admins。
    user_rows = (
        await db.execute(
            select(User.status, User.role, func.count(User.id)).group_by(
                User.status, User.role
            )
        )
    ).all()
    users_by_status: dict[str, int] = defaultdict(int)
    users_by_role: dict[str, int] = defaultdict(int)
    users_total = 0
    for status, role, count in user_rows:
        value = int(count or 0)
        users_by_status[status] += value
        users_by_role[role] += value
        users_total += value

    # 2) conversations：全表 GROUP BY status，deleted 单列。
    conversation_counts = _counts_by_status(
        (
            await db.execute(
                select(Conversation.status, func.count(Conversation.id)).group_by(
                    Conversation.status
                )
            )
        ).all()
    )

    # 3) messages：一条 COUNT。
    messages_total = int(await db.scalar(select(func.count(Message.id))) or 0)

    # 4) runs：全表 GROUP BY status，固定状态键由响应模型补齐。
    run_counts = _counts_by_status(
        (
            await db.execute(
                select(Run.status, func.count(Run.id)).group_by(Run.status)
            )
        ).all()
    )

    # 5) a2a_tasks：全表 GROUP BY state。
    a2a_counts = _counts_by_status(
        (
            await db.execute(
                select(A2aTask.state, func.count(A2aTask.id)).group_by(A2aTask.state)
            )
        ).all()
    )

    # 6) knowledge_documents：全表 GROUP BY status。
    document_counts = _counts_by_status(
        (
            await db.execute(
                select(
                    KnowledgeDocument.status, func.count(KnowledgeDocument.id)
                ).group_by(KnowledgeDocument.status)
            )
        ).all()
    )

    # 7) knowledge_chunks：一条 COUNT。
    chunks_total = int(await db.scalar(select(func.count(KnowledgeChunk.id))) or 0)

    # 8) attachments：一条 GROUP BY status + SUM(size_bytes)；deleted 不计入
    # total/storage_bytes，只单独计数。
    attachment_rows = (
        await db.execute(
            select(
                Attachment.status,
                func.count(Attachment.id),
                func.coalesce(func.sum(Attachment.size_bytes), 0),
            ).group_by(Attachment.status)
        )
    ).all()
    attachments_total = 0
    attachments_storage_bytes = 0
    attachments_deleted = 0
    for status, count, size_bytes in attachment_rows:
        status_count = int(count or 0)
        if status == "deleted":
            attachments_deleted += status_count
            continue
        attachments_total += status_count
        attachments_storage_bytes += int(size_bytes or 0)

    return AdminOverviewOut(
        generated_at=datetime.now(UTC),
        users=AdminOverviewUsers(
            total=users_total,
            active=users_by_status.get("active", 0),
            disabled=users_by_status.get("disabled", 0),
            admins=users_by_role.get("admin", 0),
        ),
        conversations=AdminOverviewConversations(
            total=sum(conversation_counts.values()),
            active=conversation_counts.get("active", 0),
            archived=conversation_counts.get("archived", 0),
            deleted=conversation_counts.get("deleted", 0),
        ),
        messages=AdminOverviewMessages(total=messages_total),
        runs=AdminOverviewRuns(
            total=sum(run_counts.values()),
            **{status: run_counts.get(status, 0) for status in _RUN_STATUSES},
        ),
        a2a_tasks=AdminOverviewA2aTasks(
            total=sum(a2a_counts.values()),
            **{state: a2a_counts.get(state, 0) for state in A2A_TASK_STATES},
        ),
        knowledge=AdminOverviewKnowledge(
            documents=sum(document_counts.values()),
            chunks=chunks_total,
            documents_pending=document_counts.get("pending", 0),
            documents_indexing=document_counts.get("indexing", 0),
            documents_ready=document_counts.get("ready", 0),
            documents_failed=document_counts.get("failed", 0),
        ),
        attachments=AdminOverviewAttachments(
            total=attachments_total,
            storage_bytes=attachments_storage_bytes,
            deleted=attachments_deleted,
        ),
    )


__all__ = [
    "build_overview",
    "get_admin_instruction",
    "get_user",
    "list_users",
    "update_admin_instruction",
    "update_user",
]
