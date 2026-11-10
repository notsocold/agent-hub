"""A2A 任务查询与动作业务。

- 列表/详情/动作全部走懒过期：等待状态超过 expires_at 自动置 canceled；
- 动作（cancel/resume/authorize/reject）写审计，并唤醒运行中的工具等待者；
- 取消对远程尽力调用 ``tasks/cancel``（远程不可达时只落本地状态）。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.cursor import decode_cursor, encode_cursor
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.db.models.chat import Run
from app.db.models.conversation import Conversation
from app.db.models.remote import A2A_ACTIVE_STATES, A2aTask, RemoteAgent
from app.db.models.user import User
from app.modules.a2a import A2aClient, A2aError
from app.modules.a2a.taskstate import (
    append_timeline,
    available_actions,
    expire_waiting,
    task_duration_ms,
    task_error_summary,
    wake_decision_for,
    wake_for_cancel,
)
from app.modules.agents.service import _resolved_headers
from app.modules.tasks.schemas import (
    A2aTaskDetail,
    A2aTaskListItem,
    TaskAuthorizeRequest,
    TaskPage,
    TaskRejectRequest,
    TaskResumeRequest,
    TaskResumeResult,
    TaskRunRef,
    TaskStats,
)

logger = logging.getLogger(__name__)

_STATES = (
    "submitted",
    "working",
    "input_required",
    "auth_required",
    "completed",
    "failed",
    "canceled",
    "rejected",
)


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- 序列化


def _request_summary(task: A2aTask) -> str:
    request = task.request or {}
    return str(request.get("summary") or "")[:500]


def _clean_result(task: A2aTask) -> dict | None:
    result = task.result
    if not isinstance(result, dict):
        return None
    artifact = result.get("artifact") if isinstance(result.get("artifact"), dict) else None
    out: dict = {"summary": str(result.get("summary") or "")[:500]}
    if artifact is None:
        out["artifact"] = {"type": "text", "summary": "", "parts": [], "data": {}}
    else:
        parts = artifact.get("parts")
        out["artifact"] = {
            "type": str(artifact.get("type") or "text")[:32],
            "summary": str(artifact.get("summary") or "")[:500],
            "parts": [str(item)[:4000] for item in parts] if isinstance(parts, list) else [],
            "data": artifact.get("data") if isinstance(artifact.get("data"), dict) else {},
        }
    return out


def _clean_error(task: A2aTask) -> dict | None:
    error = task.error
    if not isinstance(error, dict):
        return None
    at = error.get("at")
    try:
        at = datetime.fromisoformat(str(at)) if at else None
    except ValueError:
        at = None
    return {
        "code": str(error.get("code") or "a2a_error")[:64],
        "message": str(error.get("message") or "")[:500],
        "retryable": bool(error.get("retryable", False)),
        "at": at,
    }


def _waiting_out(task: A2aTask, kind: str) -> dict | None:
    payload = task.input_request if kind == "input_request" else task.auth_request
    if not isinstance(payload, dict):
        return None
    expires = payload.get("expires_at")
    try:
        expires_at = datetime.fromisoformat(str(expires)) if expires else None
    except ValueError:
        expires_at = None
    if kind == "input_request":
        schema = payload.get("input_schema")
        return {
            "question": str(payload.get("question") or "")[:2000],
            "input_schema": schema if isinstance(schema, dict) else {},
            "expires_at": expires_at,
        }
    scopes = payload.get("scopes")
    return {
        "scopes": [str(item)[:64] for item in scopes] if isinstance(scopes, list) else [],
        "authorization_url": str(payload.get("authorization_url") or "")[:2000] or None,
        "expires_at": expires_at,
    }


def _timeline_out(task: A2aTask) -> list[dict]:
    out: list[dict] = []
    for item in task.timeline or []:
        if not isinstance(item, dict):
            continue
        at = item.get("at")
        try:
            at = datetime.fromisoformat(str(at)) if at else None
        except ValueError:
            at = None
        if at is None:
            continue
        out.append(
            {
                "state": str(item.get("state") or "working")[:32],
                "at": at,
                "duration_ms": item.get("duration_ms") if isinstance(item.get("duration_ms"), int) else None,
                "note": str(item.get("note") or "")[:500],
            }
        )
    return out


def _detail_out(
    task: A2aTask,
    agent: RemoteAgent | None,
    conversation: Conversation | None,
) -> A2aTaskDetail:
    request = task.request or {}
    return A2aTaskDetail(
        id=task.id,
        a2a_task_id=task.a2a_task_id,
        agent={"id": task.remote_agent_id, "slug": agent.slug if agent else "", "name": agent.name if agent else ""},
        run_id=task.run_id,
        conversation=(
            {"id": conversation.id, "title": conversation.title} if conversation else None
        ),
        state=task.state,  # type: ignore[arg-type]
        progress=task.progress,
        duration_ms=task_duration_ms(task),
        request_summary=_request_summary(task),
        error_summary=task_error_summary(task),
        available_actions=available_actions(task.state),  # type: ignore[arg-type]
        created_at=task.created_at,
        updated_at=task.updated_at,
        tool_call_id=task.tool_call_id,
        request=request or None,
        result=_clean_result(task),
        error=_clean_error(task),
        input_request=_waiting_out(task, "input_request"),
        auth_request=_waiting_out(task, "auth_request"),
        timeline=_timeline_out(task),
        started_at=task.started_at,
        finished_at=task.finished_at,
    )


def _list_out(
    task: A2aTask,
    agent: RemoteAgent | None,
    conversation: Conversation | None,
) -> A2aTaskListItem:
    return A2aTaskListItem(
        id=task.id,
        a2a_task_id=task.a2a_task_id,
        agent={
            "id": task.remote_agent_id,
            "slug": agent.slug if agent else "",
            "name": agent.name if agent else "",
        },
        run_id=task.run_id,
        conversation=(
            {"id": conversation.id, "title": conversation.title} if conversation else None
        ),
        state=task.state,  # type: ignore[arg-type]
        progress=task.progress,
        duration_ms=task_duration_ms(task),
        request_summary=_request_summary(task),
        error_summary=task_error_summary(task),
        available_actions=available_actions(task.state),  # type: ignore[arg-type]
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


# --------------------------------------------------------------------------- 查询


async def _get_task(db: AsyncSession, task_id: uuid.UUID) -> A2aTask:
    task = await db.get(A2aTask, task_id)
    if task is None:
        raise NotFoundError("任务不存在")
    return task


async def _refs(db: AsyncSession, task: A2aTask) -> tuple[RemoteAgent | None, Conversation | None]:
    agent = await db.get(RemoteAgent, task.remote_agent_id)
    conversation = await db.get(Conversation, task.conversation_id) if task.conversation_id else None
    return agent, conversation


async def expire_stale_tasks(db: AsyncSession) -> list[A2aTask]:
    """懒过期：等待状态的过期任务置 canceled 并追加 timeline。"""
    rows = list(
        (
            await db.execute(
                select(A2aTask).where(A2aTask.state.in_(("input_required", "auth_required")))
            )
        ).scalars()
    )
    changed: list[A2aTask] = []
    for task in rows:
        if expire_waiting(task):
            changed.append(task)
            await write_audit(
                db,
                actor_type="system",
                actor_id="system",
                action="task.expired",
                resource_type="a2a_task",
                resource_id=str(task.id),
                detail={"a2a_task_id": task.a2a_task_id},
                commit=False,
            )
            wake_decision_for("expired", task)
    if changed:
        await db.commit()
    return changed


async def list_tasks(
    db: AsyncSession,
    *,
    states: list[str] | None,
    agent_id: uuid.UUID | None,
    run_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
    q: str | None,
    from_: datetime | None,
    to: datetime | None,
    limit: int,
    cursor: str | None,
) -> TaskPage:
    await expire_stale_tasks(db)
    filters = []
    if states:
        unknown = [state for state in states if state not in _STATES]
        if unknown:
            raise ValidationFailedError(f"未知任务状态：{unknown[0]}")
        filters.append(A2aTask.state.in_(states))
    if agent_id is not None:
        filters.append(A2aTask.remote_agent_id == agent_id)
    if run_id is not None:
        filters.append(A2aTask.run_id == run_id)
    if conversation_id is not None:
        filters.append(A2aTask.conversation_id == conversation_id)
    if q:
        pattern = f"%{q}%"
        filters.append(
            or_(
                A2aTask.a2a_task_id.ilike(pattern),
                A2aTask.request["summary"].astext.ilike(pattern),
            )
        )
    if from_ is not None:
        filters.append(A2aTask.created_at >= from_)
    if to is not None:
        filters.append(A2aTask.created_at <= to)

    decoded = decode_cursor(cursor)
    if decoded.get("created_at") and decoded.get("id"):
        try:
            since = datetime.fromisoformat(str(decoded["created_at"]))
            filters.append(
                or_(
                    A2aTask.created_at < since,
                    and_(A2aTask.created_at == since, A2aTask.id < uuid.UUID(str(decoded["id"]))),
                )
            )
        except ValueError as exc:
            raise ValidationFailedError("分页游标无效") from exc

    stmt = (
        select(A2aTask)
        .where(*filters)
        .order_by(A2aTask.created_at.desc(), A2aTask.id.desc())
        .limit(limit + 1)
    )
    rows = list((await db.execute(stmt)).scalars())
    has_more = len(rows) > limit
    rows = rows[:limit]

    agent_ids = {item.remote_agent_id for item in rows}
    conversation_ids = {item.conversation_id for item in rows if item.conversation_id}
    agents = {
        item.id: item
        for item in (
            await db.execute(select(RemoteAgent).where(RemoteAgent.id.in_(agent_ids)))
        ).scalars()
    } if agent_ids else {}
    conversations = {
        item.id: item
        for item in (
            await db.execute(
                select(Conversation).where(Conversation.id.in_(conversation_ids))
            )
        ).scalars()
    } if conversation_ids else {}

    items = [
        _list_out(item, agents.get(item.remote_agent_id), conversations.get(item.conversation_id))
        for item in rows
    ]
    stats = await _task_stats(db, filters)
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor({"created_at": last.created_at, "id": last.id})
    return TaskPage(items=items, next_cursor=next_cursor, has_more=has_more, stats=stats)


async def _task_stats(db: AsyncSession, filters: list) -> TaskStats:
    stats = TaskStats()
    base = filters or []
    rows = await db.execute(
        select(A2aTask.state, func.count()).where(*base).group_by(A2aTask.state)
    )
    for key, count in rows.all():
        if key in _STATES:
            setattr(stats, str(key), int(count))
    stats.total = sum(
        getattr(stats, key) for key in _STATES
    )
    return stats


async def get_task_detail(db: AsyncSession, task_id: uuid.UUID) -> A2aTaskDetail:
    await expire_stale_tasks(db)
    task = await _get_task(db, task_id)
    agent, conversation = await _refs(db, task)
    return _detail_out(task, agent, conversation)


# --------------------------------------------------------------------------- 动作


async def cancel_task_action(db: AsyncSession, user: User, task_id: uuid.UUID) -> A2aTaskDetail:
    await expire_stale_tasks(db)
    task = await _get_task(db, task_id)
    if task.state not in A2A_ACTIVE_STATES:
        raise ConflictError("任务当前状态无法取消", code="task_not_cancelable")
    agent = await db.get(RemoteAgent, task.remote_agent_id)
    if task.a2a_task_id and agent is not None:
        try:
            client = A2aClient(
                agent.url, headers=_resolved_headers(agent), card=agent.card
            )
            await client.cancel_task(task.a2a_task_id)
        except A2aError as exc:
            logger.info("远程取消失败（尽力而为）task=%s：%s", task.id, exc.message)
    append_timeline(task, "canceled", note="用户取消")
    wake_for_cancel(task)
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="task.canceled",
        resource_type="a2a_task",
        resource_id=str(task.id),
        detail={"a2a_task_id": task.a2a_task_id},
    )
    await db.commit()
    await db.refresh(task)
    conversation = await db.get(Conversation, task.conversation_id) if task.conversation_id else None
    return _detail_out(task, agent, conversation)


async def resume_task_action(
    db: AsyncSession, user: User, task_id: uuid.UUID, payload: TaskResumeRequest
) -> TaskResumeResult:
    await expire_stale_tasks(db)
    task = await _get_task(db, task_id)
    if task.state != "input_required":
        raise ConflictError("任务当前状态无法补充输入", code="task_not_resumable")
    agent, conversation = await _refs(db, task)
    # 记录补充输入但不改变状态：等待中的工具执行会用同一 a2a_task_id 继续远程调用。
    append_timeline(task, "input_required", note=f"已补充：{payload.input[:100]}")
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="task.resumed",
        resource_type="a2a_task",
        resource_id=str(task.id),
        detail={"a2a_task_id": task.a2a_task_id},
    )
    await db.commit()
    await db.refresh(task)
    wake_decision_for("resume", task, {"input": payload.input})
    run_ref: TaskRunRef | None = None
    if task.run_id is not None:
        run = await db.get(Run, task.run_id)
        if run is not None:
            run_ref = TaskRunRef(id=run.id, status=run.status)
    return TaskResumeResult(task=_detail_out(task, agent, conversation), run=run_ref)


async def authorize_task_action(
    db: AsyncSession, user: User, task_id: uuid.UUID, payload: TaskAuthorizeRequest
) -> A2aTaskDetail:
    await expire_stale_tasks(db)
    task = await _get_task(db, task_id)
    if task.state != "auth_required":
        raise ConflictError("任务当前状态无法授权", code="task_not_authorizable")
    agent, conversation = await _refs(db, task)
    note = (payload.note or "").strip()
    append_timeline(task, "auth_required", note=f"已授权{'：' + note if note else ''}")
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="task.authorized",
        resource_type="a2a_task",
        resource_id=str(task.id),
        detail={"a2a_task_id": task.a2a_task_id, "scopes": payload.scopes},
    )
    await db.commit()
    await db.refresh(task)
    wake_decision_for("authorize", task)
    return _detail_out(task, agent, conversation)


async def reject_task_action(
    db: AsyncSession, user: User, task_id: uuid.UUID, payload: TaskRejectRequest
) -> A2aTaskDetail:
    await expire_stale_tasks(db)
    task = await _get_task(db, task_id)
    if task.state not in {"auth_required", "input_required"}:
        raise ConflictError("任务当前状态无法拒绝", code="task_not_rejectable")
    agent, conversation = await _refs(db, task)
    reason = (payload.reason or "").strip()
    append_timeline(task, "rejected", note=reason[:200] or "用户拒绝")
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="task.rejected",
        resource_type="a2a_task",
        resource_id=str(task.id),
        detail={"a2a_task_id": task.a2a_task_id, "reason": reason[:200]},
    )
    await db.commit()
    await db.refresh(task)
    wake_decision_for("reject", task)
    return _detail_out(task, agent, conversation)


__all__ = [
    "authorize_task_action",
    "cancel_task_action",
    "expire_stale_tasks",
    "get_task_detail",
    "list_tasks",
    "reject_task_action",
    "resume_task_action",
]
