"""知识库数据访问层（repository）。

只负责 SQL 与行读写：查询、游标分页、统计、批量插块、删除分块。
权限判断、失败处理、事务边界都在 service 层，保持数据访问与编排分离。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, delete, func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cursor import decode_cursor
from app.core.errors import ValidationFailedError
from app.db.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.db.models.user import User


def visible_filter(user: User) -> list[ColumnElement[bool]]:
    """可见性条件：系统文档（``owner_user_id IS NULL``）+ 调用者自己的文档。"""
    return [
        or_(
            KnowledgeDocument.owner_user_id.is_(None),
            KnowledgeDocument.owner_user_id == user.id,
        )
    ]


def scope_filter(user: User, owner_scope: str) -> list[ColumnElement[bool]]:
    """归属范围条件。

    ``all`` 由 service 校验管理员身份后使用；这里只按字面拼 SQL。
    """
    return [] if owner_scope == "all" else visible_filter(user)


def _cursor_filter(cursor: str | None) -> list[ColumnElement[bool]]:
    """把 ``{"t": updated_at, "id": id}`` 游标转成 ``updated_at DESC, id DESC`` 的续页条件。"""
    payload = decode_cursor(cursor)
    if not payload:
        return []
    try:
        updated_at = datetime.fromisoformat(str(payload["t"]))
        document_id = uuid.UUID(str(payload["id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationFailedError("分页游标无效") from exc
    # 与排序键完全一致：先比时间，时间相同再比 id（两者都降序）。
    return [
        or_(
            KnowledgeDocument.updated_at < updated_at,
            and_(
                KnowledgeDocument.updated_at == updated_at,
                KnowledgeDocument.id < document_id,
            ),
        )
    ]


async def find_by_checksum(
    db: AsyncSession, *, owner_user_id: uuid.UUID | None, checksum: str
) -> KnowledgeDocument | None:
    """按归属 + checksum 查重；系统文档与用户文档各成唯一域。"""
    stmt = select(KnowledgeDocument).where(KnowledgeDocument.checksum == checksum)
    if owner_user_id is None:
        stmt = stmt.where(KnowledgeDocument.owner_user_id.is_(None))
    else:
        stmt = stmt.where(KnowledgeDocument.owner_user_id == owner_user_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_document(db: AsyncSession, document_id: uuid.UUID) -> KnowledgeDocument | None:
    return await db.get(KnowledgeDocument, document_id)


async def lock_document(db: AsyncSession, document_id: uuid.UUID) -> KnowledgeDocument | None:
    """用 ``FOR UPDATE`` 取文档行，串行化同一文档的并发重建。"""
    stmt = select(KnowledgeDocument).where(KnowledgeDocument.id == document_id).with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_documents(
    db: AsyncSession,
    *,
    user: User,
    q: str | None,
    category: str | None,
    status: str | None,
    owner_scope: str,
    limit: int,
    cursor: str | None,
) -> tuple[list[KnowledgeDocument], bool]:
    """返回一页文档（最多 ``limit`` 条）与是否还有下一页。

    多取一条用于判断 ``has_more``，避免额外 COUNT 查询。
    """
    conditions = scope_filter(user, owner_scope)
    if q:
        pattern = f"%{q}%"
        conditions.append(
            or_(
                KnowledgeDocument.title.ilike(pattern),
                KnowledgeDocument.source_uri.ilike(pattern),
            )
        )
    if category:
        conditions.append(KnowledgeDocument.category == category)
    if status:
        conditions.append(KnowledgeDocument.status == status)
    conditions.extend(_cursor_filter(cursor))

    stmt = (
        select(KnowledgeDocument)
        .where(*conditions)
        .order_by(KnowledgeDocument.updated_at.desc(), KnowledgeDocument.id.desc())
        .limit(limit + 1)
    )
    rows = list((await db.execute(stmt)).scalars())
    return rows[:limit], len(rows) > limit


async def document_stats(db: AsyncSession, *, user: User, owner_scope: str) -> dict[str, int]:
    """按归属范围统计文档与分块，忽略列表的 q/category/status 过滤。"""
    stmt = select(
        func.count(KnowledgeDocument.id),
        func.coalesce(func.sum(KnowledgeDocument.chunk_count), 0),
        func.count(KnowledgeDocument.id).filter(KnowledgeDocument.status == "ready"),
        func.count(KnowledgeDocument.id).filter(KnowledgeDocument.status == "indexing"),
        func.count(KnowledgeDocument.id).filter(KnowledgeDocument.status == "pending"),
        func.count(KnowledgeDocument.id).filter(KnowledgeDocument.status == "failed"),
    ).where(*scope_filter(user, owner_scope))
    row = (await db.execute(stmt)).one()
    return {
        "documents": int(row[0] or 0),
        "chunks": int(row[1] or 0),
        "ready": int(row[2] or 0),
        "indexing": int(row[3] or 0),
        "pending": int(row[4] or 0),
        "failed": int(row[5] or 0),
    }


async def documents_for_rebuild(db: AsyncSession, *, scope: str) -> list[KnowledgeDocument]:
    """列出待重建文档；``scope`` 只接受 all/system/own（service 已校验）。"""
    stmt = select(KnowledgeDocument)
    if scope == "system":
        stmt = stmt.where(KnowledgeDocument.owner_user_id.is_(None))
    elif scope == "own":
        stmt = stmt.where(KnowledgeDocument.owner_user_id.is_not(None))
    # 固定加锁顺序，降低两个并发重建互相死锁的概率。
    stmt = stmt.order_by(KnowledgeDocument.id.asc())
    return list((await db.execute(stmt)).scalars())


async def delete_chunks(db: AsyncSession, document_id: uuid.UUID) -> None:
    await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id))


async def insert_chunks(db: AsyncSession, rows: Sequence[dict[str, Any]]) -> None:
    """批量插入分块；空列表直接跳过，避免生成非法 SQL。"""
    if not rows:
        return
    await db.execute(insert(KnowledgeChunk), list(rows))


__all__ = [
    "delete_chunks",
    "document_stats",
    "documents_for_rebuild",
    "find_by_checksum",
    "get_document",
    "insert_chunks",
    "list_documents",
    "lock_document",
    "scope_filter",
    "visible_filter",
]
