"""M9 工作流 CRUD 业务：创建/列表/详情可见性/更新/启停/硬删（运行历史由 T4 追加）。

- 可见性：``get_visible_workflow`` 返回「系统模板或本人模板」的行，否则 None；
  404 由调用方抛（T5 chat 校验复用同一语义，不泄露他人模板存在性）。
- 越权面：写操作统一走 ``_require_owned``——仅本人 ``scope='user'`` 模板可改；
  系统模板（owner 为 NULL）与他人模板同样按不存在处理（404）。
- steps 校验唯一口径是装配器 ``validate_steps``（8000 预算与 config 结构耦合，
  不得扩展）：错误列表非空即 422 ``workflow_steps_invalid``（消息拼前 3 条），
  CRUD 层不自造结构校验，``config`` 键原样透传。
- 版本规则：PATCH 成功必 ``version += 1``（含仅改 name/description）；
  ``PUT enabled`` 不改 version；DELETE 硬删（``workflow_runs`` 随 FK CASCADE）。
- 游标分页：系统模板 + 本人按 ``created_at DESC, id DESC``，limit+1 探测，
  复用 ``encode_cursor({"created_at": …, "id": …})`` 模式。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.cursor import decode_cursor, encode_cursor
from app.core.errors import NotFoundError, ValidationFailedError
from app.db.models.user import User
from app.db.models.workflow import Workflow
from app.modules.workflows.assembler import validate_steps
from app.modules.workflows.schemas import WorkflowListOut, WorkflowOut

# 422 消息最多拼接的装配器错误条数（避免超长响应）。
_MAX_ERROR_MESSAGES = 3

# 创建时必写字段（description 缺省写空串）；审计 detail 只放字段名。
_CREATION_FIELDS = ("description", "name", "steps")


def _raise_invalid_steps(errors: list[str]) -> None:
    """把装配器错误列表转成 422 ``workflow_steps_invalid``（消息拼前 3 条）。"""
    shown = "；".join(errors[:_MAX_ERROR_MESSAGES])
    raise ValidationFailedError(f"工作流步骤不合法：{shown}", code="workflow_steps_invalid")


def _to_out(row: Workflow) -> WorkflowOut:
    return WorkflowOut(
        id=row.id,
        scope=row.scope,  # type: ignore[arg-type]
        name=row.name,
        description=row.description,
        steps=list(row.steps or []),
        enabled=row.enabled,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def get_visible_workflow(
    db: AsyncSession, user: User, workflow_id: uuid.UUID
) -> Workflow | None:
    """返回「系统模板或本人模板」的行，否则 None；404 由调用方抛。

    语义契约（T5 复用做 chat 校验）：他人 user 模板与不存在同样返回 None，
    不泄露存在性。
    """
    row = await db.get(Workflow, workflow_id)
    if row is None:
        return None
    if row.scope == "system" or row.owner_user_id == user.id:
        return row
    return None


async def _require_owned(db: AsyncSession, user: User, workflow_id: uuid.UUID) -> Workflow:
    """写操作的可见性 + 归属校验：仅本人 user 模板可改，其余一律 404。"""
    row = await get_visible_workflow(db, user, workflow_id)
    # 系统模板 owner 为 NULL（!= user.id）与他人模板同样按不存在处理。
    if row is None or row.scope != "user" or row.owner_user_id != user.id:
        raise NotFoundError("工作流不存在")
    return row


async def create_workflow(
    db: AsyncSession,
    user: User,
    *,
    name: str,
    description: str,
    steps: list[Any],
) -> WorkflowOut:
    """创建本人 user 模板：version=1、enabled=True；steps 先过装配器校验。"""
    errors = validate_steps(steps)
    if errors:
        _raise_invalid_steps(errors)
    row = Workflow(
        scope="user",
        owner_user_id=user.id,
        name=name,
        description=description,
        steps=steps,
    )
    db.add(row)
    await db.flush()  # 先取主键，审计 resource_id 才有值
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="workflow.created",
        resource_type="workflow",
        resource_id=str(row.id),
        detail={"fields": list(_CREATION_FIELDS)},
    )
    await db.commit()
    await db.refresh(row)
    return _to_out(row)


async def get_workflow(db: AsyncSession, user: User, workflow_id: uuid.UUID) -> WorkflowOut:
    """详情：系统或本人 → 200；他人/不存在 → 404（不泄露存在性）。"""
    row = await get_visible_workflow(db, user, workflow_id)
    if row is None:
        raise NotFoundError("工作流不存在")
    return _to_out(row)


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
        Workflow.created_at < last_created_at,
        and_(Workflow.created_at == last_created_at, Workflow.id < last_id),
    )


async def list_workflows(
    db: AsyncSession, user: User, *, limit: int, cursor: str | None
) -> WorkflowListOut:
    """系统模板 + 本人模板按 ``created_at DESC, id DESC`` 游标分页。"""
    visibility = or_(Workflow.scope == "system", Workflow.owner_user_id == user.id)
    cursor_condition = _cursor_filter(cursor)
    filters = [visibility] if cursor_condition is None else [visibility, cursor_condition]
    stmt = (
        select(Workflow)
        .where(*filters)
        .order_by(Workflow.created_at.desc(), Workflow.id.desc())
        .limit(limit + 1)
    )
    rows = list((await db.execute(stmt)).scalars())
    has_more = len(rows) > limit
    rows = rows[:limit]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor({"created_at": last.created_at, "id": last.id})
    return WorkflowListOut(
        items=[_to_out(row) for row in rows],
        next_cursor=next_cursor,
        has_more=has_more,
    )


async def update_workflow(
    db: AsyncSession,
    user: User,
    workflow_id: uuid.UUID,
    *,
    name: str | None,
    description: str | None,
    steps: list[Any] | None,
) -> WorkflowOut:
    """PATCH 本人 user 模板：成功必 ``version += 1``（含仅改 name/description）。"""
    row = await _require_owned(db, user, workflow_id)
    if steps is not None:
        errors = validate_steps(steps)
        if errors:
            _raise_invalid_steps(errors)

    changed: list[str] = []
    if name is not None:
        row.name = name
        changed.append("name")
    if description is not None:
        row.description = description
        changed.append("description")
    if steps is not None:
        row.steps = steps
        changed.append("steps")
    row.version += 1

    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="workflow.updated",
        resource_type="workflow",
        resource_id=str(row.id),
        detail={"fields": sorted(changed)},
    )
    await db.commit()
    await db.refresh(row)
    return _to_out(row)


async def set_enabled(
    db: AsyncSession, user: User, workflow_id: uuid.UUID, *, enabled: bool
) -> WorkflowOut:
    """启停本人模板：只改 ``enabled`` 位，不改 version。"""
    row = await _require_owned(db, user, workflow_id)
    row.enabled = enabled
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="workflow.enabled",
        resource_type="workflow",
        resource_id=str(row.id),
        detail={"enabled": enabled},
    )
    await db.commit()
    await db.refresh(row)
    return _to_out(row)


async def delete_workflow(db: AsyncSession, user: User, workflow_id: uuid.UUID) -> None:
    """硬删本人模板；``workflow_runs`` 随 FK CASCADE 一并删除。"""
    row = await _require_owned(db, user, workflow_id)
    await db.delete(row)
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="workflow.deleted",
        resource_type="workflow",
        resource_id=str(workflow_id),
    )
    await db.commit()


__all__ = [
    "create_workflow",
    "delete_workflow",
    "get_visible_workflow",
    "get_workflow",
    "list_workflows",
    "set_enabled",
    "update_workflow",
]
