"""长期记忆业务：CRUD、清空、导出与聊天注入。

- q 在 M1.5 用 ILIKE 检索（语义检索在 M4 接入 embedding 后替换）；
- 保存去重 v1 用内容完全一致的 active 条目合并（语义去重后置）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import content_fingerprint, write_audit
from app.core.config import get_settings
from app.core.cursor import decode_cursor, encode_cursor
from app.core.errors import CapabilityDisabledError, NotFoundError, ValidationFailedError
from app.db.models.memory import Memory
from app.db.models.user import User
from app.modules.memories.schemas import (
    MemoryCreateRequest,
    MemoryExportOut,
    MemoryOut,
    MemoryPage,
    MemoryUpdateRequest,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _memory_out(memory: Memory) -> MemoryOut:
    return MemoryOut(
        id=memory.id,
        kind=memory.kind,  # type: ignore[arg-type]
        title=memory.title,
        content=memory.content,
        pinned=memory.pinned,
        status=memory.status,  # type: ignore[arg-type]
        source=memory.source or {},
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


def _require_enabled() -> None:
    if not get_settings().memories_enabled:
        raise CapabilityDisabledError("记忆能力未启用")


async def _stats(db: AsyncSession, user: User) -> dict[str, int]:
    rows = (
        await db.execute(
            select(Memory.kind, Memory.status, func.count(Memory.id))
            .where(Memory.user_id == user.id)
            .group_by(Memory.kind, Memory.status)
        )
    ).all()
    stats = {"total": 0, "profile": 0, "preference": 0, "fact": 0, "active": 0, "archived": 0}
    for kind, status, count in rows:
        stats["total"] += int(count)
        if kind in stats:
            stats[kind] += int(count)
        if status in stats:
            stats[status] += int(count)
    return stats


async def list_memories(
    db: AsyncSession,
    user: User,
    *,
    kind: str | None,
    status: str | None,
    q: str | None,
    limit: int,
    cursor: str | None,
) -> MemoryPage:
    _require_enabled()
    stmt = select(Memory).where(Memory.user_id == user.id)
    if kind in {"profile", "preference", "fact"}:
        stmt = stmt.where(Memory.kind == kind)
    if status in {"active", "archived"}:
        stmt = stmt.where(Memory.status == status)
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(or_(Memory.content.ilike(pattern), Memory.title.ilike(pattern)))
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
                Memory.updated_at < last_time,
                and_(Memory.updated_at == last_time, Memory.id < last_id),
            )
        )
    stmt = stmt.order_by(Memory.updated_at.desc(), Memory.id.desc()).limit(limit + 1)
    rows = list((await db.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        next_cursor = encode_cursor({"t": rows[-1].updated_at.isoformat(), "id": rows[-1].id})
    return MemoryPage(
        items=[_memory_out(row) for row in rows],
        next_cursor=next_cursor,
        has_more=has_more,
        stats=await _stats(db, user),
    )


async def create_memory(
    db: AsyncSession,
    user: User,
    payload: MemoryCreateRequest,
    *,
    source: dict[str, Any] | None = None,
) -> MemoryOut:
    _require_enabled()
    settings = get_settings()
    if len(payload.content) > settings.memory_max_item_chars:
        raise ValidationFailedError(f"记忆内容超过 {settings.memory_max_item_chars} 字符上限")
    memory = Memory(
        user_id=user.id,
        kind=payload.kind,
        title=payload.title,
        content=payload.content,
        pinned=payload.pinned,
        status="active",
        source=source or {},
    )
    db.add(memory)
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="memory.create",
        resource_type="memory",
        resource_id=str(memory.id),
        detail={"kind": payload.kind, "length": len(payload.content), "fingerprint": content_fingerprint(payload.content)},
    )
    await db.commit()
    await db.refresh(memory)
    return _memory_out(memory)


async def _load_owned(db: AsyncSession, user: User, memory_id: uuid.UUID) -> Memory:
    _require_enabled()
    memory = await db.get(Memory, memory_id)
    if memory is None or memory.user_id != user.id:
        raise NotFoundError("记忆不存在")
    return memory


async def get_memory(db: AsyncSession, user: User, memory_id: uuid.UUID) -> MemoryOut:
    return _memory_out(await _load_owned(db, user, memory_id))


async def update_memory(
    db: AsyncSession, user: User, memory_id: uuid.UUID, payload: MemoryUpdateRequest
) -> MemoryOut:
    memory = await _load_owned(db, user, memory_id)
    settings = get_settings()
    if payload.content is not None:
        if len(payload.content) > settings.memory_max_item_chars:
            raise ValidationFailedError(
                f"记忆内容超过 {settings.memory_max_item_chars} 字符上限"
            )
        memory.content = payload.content
    if payload.kind is not None:
        memory.kind = payload.kind
    if payload.title is not None:
        memory.title = payload.title.strip()
    if payload.pinned is not None:
        if payload.pinned and memory.status == "active":
            pinned_count = await db.scalar(
                select(func.count(Memory.id)).where(
                    Memory.user_id == user.id,
                    Memory.pinned.is_(True),
                    Memory.status == "active",
                    Memory.id != memory.id,
                )
            )
            if int(pinned_count or 0) >= settings.memory_pinned_max:
                raise ValidationFailedError(
                    f"置顶记忆最多 {settings.memory_pinned_max} 条", code="memory_pinned_limit"
                )
        memory.pinned = payload.pinned
    if payload.status is not None:
        memory.status = payload.status
        if payload.status == "archived":
            memory.pinned = False
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="memory.update",
        resource_type="memory",
        resource_id=str(memory_id),
        detail={
            "length": len(memory.content),
            "fingerprint": content_fingerprint(memory.content),
        },
    )
    await db.commit()
    await db.refresh(memory)
    return _memory_out(memory)


async def delete_memory(db: AsyncSession, user: User, memory_id: uuid.UUID) -> None:
    memory = await _load_owned(db, user, memory_id)
    await db.delete(memory)
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="memory.delete",
        resource_type="memory",
        resource_id=str(memory_id),
        detail={"kind": memory.kind},
    )
    await db.commit()


async def clear_memories(db: AsyncSession, user: User) -> int:
    _require_enabled()
    count = await db.scalar(
        select(func.count(Memory.id)).where(Memory.user_id == user.id)
    )
    await db.execute(delete(Memory).where(Memory.user_id == user.id))
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="memory.clear",
        resource_type="memory",
        resource_id=str(user.id),
        detail={"count": int(count or 0)},
    )
    await db.commit()
    return int(count or 0)


async def export_memories(db: AsyncSession, user: User) -> MemoryExportOut:
    _require_enabled()
    rows = list(
        (
            await db.execute(
                select(Memory)
                .where(Memory.user_id == user.id)
                .order_by(Memory.updated_at.desc(), Memory.id.desc())
            )
        ).scalars()
    )
    return MemoryExportOut(exported_at=_now(), items=[_memory_out(row) for row in rows])


async def active_memories_for_injection(db: AsyncSession, user: User, max_tokens: int) -> list[Memory]:
    """聊天装配用：置顶在前，其余按更新时间倒序，按保守 token 预算截断。"""
    if not get_settings().memories_enabled:
        return []
    rows = list(
        (
            await db.execute(
                select(Memory)
                .where(Memory.user_id == user.id, Memory.status == "active")
                .order_by(Memory.pinned.desc(), Memory.updated_at.desc(), Memory.id.desc())
                .limit(200)
            )
        ).scalars()
    )
    picked: list[Memory] = []
    budget = max_tokens
    for row in rows:
        cost = _estimate_tokens(row.content)
        if budget - cost < 0 and picked:
            break
        picked.append(row)
        budget -= cost
    return picked


def _estimate_tokens(text: str) -> int:
    """保守估算：CJK 1 token/字，其余 0.3 token/字符。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + max(1, int((len(text) - cjk) * 0.3))


async def save_memory_for_tool(
    db: AsyncSession,
    user: User,
    *,
    content: str,
    kind: str,
    title: str | None,
    pinned: bool | None,
) -> tuple[Memory, str]:
    """save_memory 原生工具调用；内容完全一致的 active 条目合并更新。"""
    existing = await db.scalar(
        select(Memory)
        .where(
            Memory.user_id == user.id,
            Memory.status == "active",
            Memory.content == content,
        )
        .order_by(Memory.updated_at.desc())
        .limit(1)
    )
    if existing is not None:
        existing.updated_at = _now()
        if kind:
            existing.kind = kind
        if title:
            existing.title = title
        await write_audit(
            db,
            actor_type="agent",
            actor_id=str(user.id),
            action="memory.update",
            resource_type="memory",
            resource_id=str(existing.id),
            detail={"length": len(content), "fingerprint": content_fingerprint(content)},
        )
        await db.commit()
        return existing, "updated"
    payload = MemoryCreateRequest(
        kind=kind or "fact", title=title or "", content=content, pinned=bool(pinned)
    )
    memory = await create_memory(
        db,
        user,
        payload,
        source={"created_by": "agent"},
    )
    return await _load_owned(db, user, memory.id), "created"
