"""审批查询与决定。

M1 只有数据模型与接口；能触发 interrupt 的工具在 M2/M5 接入，
届时批准后通过 Command(resume=...) 继续同一条 run。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cursor import decode_cursor, encode_cursor
from app.core.errors import (
    ApprovalAlreadyDecidedError,
    ApprovalExpiredError,
    NotFoundError,
    ValidationFailedError,
)
from app.db.models.approval import Approval
from app.db.models.chat import Run
from app.db.models.conversation import Conversation
from app.db.models.user import User
from app.db.session import get_session_factory
from app.modules.approvals.schemas import (
    ApprovalDeciderOut,
    ApprovalDecisionRequest,
    ApprovalDecisionResult,
    ApprovalOut,
    ApprovalPage,
)

_STATUSES = ("pending", "approved", "rejected", "expired", "canceled")


def _now() -> datetime:
    return datetime.now(UTC)


def _approval_out(
    approval: Approval,
    conversation: Conversation,
    decider: User | None,
) -> ApprovalOut:
    return ApprovalOut(
        id=approval.id,
        run_id=approval.run_id,
        conversation_id=conversation.id,
        conversation_title=conversation.title,
        status=approval.status,  # type: ignore[arg-type]
        tool_name=approval.tool_name,
        tool_display_name=approval.tool_display_name,
        source=approval.source,
        executor=approval.executor,
        target=approval.target,
        side_effect=approval.side_effect,
        risk=approval.risk,  # type: ignore[arg-type]
        reason=approval.reason,
        payload=approval.payload or {},
        payload_preview=approval.payload_preview,
        expires_at=approval.expires_at,
        created_at=approval.created_at,
        decided_at=approval.decided_at,
        decided_by=(
            ApprovalDeciderOut(id=decider.id, display_name=decider.display_name)
            if decider
            else None
        ),
        decision_note=approval.decision_note,
    )


def _base_query():
    return (
        select(Approval, Conversation, User)
        .join(Run, Approval.run_id == Run.id)
        .join(Conversation, Run.conversation_id == Conversation.id)
        .outerjoin(User, Approval.decided_by == User.id)
    )


async def list_approvals(
    db: AsyncSession,
    user: User,
    *,
    status: str,
    conversation_id: uuid.UUID | None,
    run_id: uuid.UUID | None,
    limit: int,
    cursor: str | None,
) -> ApprovalPage:
    stmt = _base_query().where(Conversation.user_id == user.id)
    if status in _STATUSES:
        stmt = stmt.where(Approval.status == status)
    if conversation_id is not None:
        stmt = stmt.where(Run.conversation_id == conversation_id)
    if run_id is not None:
        stmt = stmt.where(Approval.run_id == run_id)
    cursor_data = decode_cursor(cursor)
    if cursor_data:
        raw_time = cursor_data.get("t")
        raw_id = cursor_data.get("id")
        try:
            last_time = datetime.fromisoformat(raw_time) if raw_time else None
            last_id = uuid.UUID(str(raw_id)) if raw_id else None
        except (ValueError, TypeError) as exc:
            raise ValidationFailedError("分页游标无效") from exc
        if last_time is None or last_id is None:
            raise ValidationFailedError("分页游标无效")
        stmt = stmt.where(
            or_(
                Approval.created_at < last_time,
                and_(Approval.created_at == last_time, Approval.id < last_id),
            )
        )
    stmt = stmt.order_by(Approval.created_at.desc(), Approval.id.desc()).limit(limit + 1)
    rows = (await db.execute(stmt)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        _approval_out(row[0], row[1], row[2] if isinstance(row[2], User) else None) for row in rows
    ]
    next_cursor = None
    if has_more and rows:
        last = rows[-1][0]
        next_cursor = encode_cursor({"t": last.created_at.isoformat(), "id": last.id})

    stats_rows = (
        await db.execute(
            select(Approval.status, func.count(Approval.id))
            .join(Run, Approval.run_id == Run.id)
            .join(Conversation, Run.conversation_id == Conversation.id)
            .where(Conversation.user_id == user.id)
            .group_by(Approval.status)
        )
    ).all()
    stats = {key: 0 for key in _STATUSES}
    for key, count in stats_rows:
        if key in stats:
            stats[key] = int(count)
    return ApprovalPage(items=items, next_cursor=next_cursor, has_more=has_more, stats=stats)


async def get_approval(db: AsyncSession, user: User, approval_id: uuid.UUID) -> ApprovalOut:
    row = (
        await db.execute(
            _base_query().where(Approval.id == approval_id, Conversation.user_id == user.id)
        )
    ).first()
    if row is None:
        raise NotFoundError("审批不存在")
    return _approval_out(row[0], row[1], row[2] if isinstance(row[2], User) else None)


async def decide(
    db: AsyncSession,
    user: User,
    approval_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
) -> ApprovalDecisionResult:
    row = (
        await db.execute(
            select(Approval)
            .join(Run, Approval.run_id == Run.id)
            .join(Conversation, Run.conversation_id == Conversation.id)
            .where(Approval.id == approval_id, Conversation.user_id == user.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("审批不存在")
    approval = row
    now = _now()
    if approval.status == "pending" and approval.expires_at and approval.expires_at <= now:
        approval.status = "expired"
        await db.commit()
        raise ApprovalExpiredError("审批已过期")
    if approval.status != "pending":
        if approval.status == "expired":
            raise ApprovalExpiredError("审批已过期")
        raise ApprovalAlreadyDecidedError("审批已处理，请刷新状态")

    approval.status = "approved" if payload.decision == "approve" else "rejected"
    approval.decided_by = user.id
    approval.decided_at = now
    approval.decision_note = payload.note
    run = await db.get(Run, approval.run_id)
    resumed = False
    resume_error: str | None = None
    if payload.decision == "approve":
        if run is not None and run.status == "interrupted":
            resume_error = "当前版本尚未接入可恢复工具，请在 M2 后重试"
        else:
            resume_error = "运行不处于等待审批状态"
    await db.commit()
    return ApprovalDecisionResult(
        id=approval.id,
        status=approval.status,  # type: ignore[arg-type]
        run_id=approval.run_id,
        resumed=resumed,
        resume_error=resume_error,
    )


async def expire_pending(limit: int = 200) -> int:
    """后台巡检：把超时未处理的审批置为 expired。"""
    now = _now()
    async with get_session_factory()() as db:
        ids = list(
            (
                await db.execute(
                    select(Approval.id)
                    .where(
                        Approval.status == "pending",
                        Approval.expires_at.is_not(None),
                        Approval.expires_at <= now,
                    )
                    .limit(limit)
                )
            ).scalars()
        )
        if not ids:
            return 0
        await db.execute(update(Approval).where(Approval.id.in_(ids)).values(status="expired"))
        await db.commit()
        return len(ids)
