"""知识库向量检索层：pgvector 相似度 SQL 只出现在这里。

业务层（service / router / chat tool）只调用 :func:`search_chunks`，不直接写 ``<=>``。
检索范围恒为「系统文档 + 调用者自己的文档」，管理员也不例外（设计文档 §7.4）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.knowledge import KnowledgeChunk, KnowledgeDocument


@dataclass
class SearchHit:
    """单条检索命中；字段与前端 ``KnowledgeSearchHit`` 一一对应（设计文档 §7.4）。"""

    chunk_id: UUID
    document_id: UUID
    chunk_index: int
    content: str
    heading: str
    document_title: str
    document_filename: str
    category: str
    updated_at: datetime
    score: float


def _heading(metadata: dict[str, Any] | None, document_title: str) -> str:
    """片段标题取 ``metadata["heading"]``，缺失或空串时回退文档标题。"""
    value = (metadata or {}).get("heading")
    if isinstance(value, str) and value.strip():
        return value
    return document_title


async def search_chunks(
    db: AsyncSession,
    *,
    embedding: list[float],
    user_id: UUID,
    top_k: int,
    threshold: float,
    categories: list[str] | None,
) -> list[SearchHit]:
    """按余弦相似度检索分块，返回分数从高到低的最多 ``top_k`` 条。

    - 仅检索 ``status = 'ready'`` 的文档；
    - 可见性：系统文档（``owner_user_id IS NULL``）+ ``user_id`` 自己的文档；
    - ``threshold > 0`` 时过滤低于阈值的命中（fake 模式调用方传 0.0）；
    - ``categories`` 非空时只保留这些分类。
    """
    # pgvector 的 ``<=>`` 余弦距离；向量以绑定参数传入，无字符串拼接。
    distance = KnowledgeChunk.embedding.cosine_distance(embedding)
    score = 1 - distance

    stmt = (
        select(
            KnowledgeChunk.id.label("chunk_id"),
            KnowledgeChunk.document_id,
            KnowledgeChunk.chunk_index,
            KnowledgeChunk.content,
            KnowledgeChunk.metadata_.label("metadata"),
            KnowledgeDocument.title.label("document_title"),
            KnowledgeDocument.filename.label("document_filename"),
            KnowledgeDocument.category,
            KnowledgeDocument.updated_at,
            score.label("score"),
        )
        .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
        .where(KnowledgeDocument.status == "ready")
        .where(
            or_(
                KnowledgeDocument.owner_user_id.is_(None),
                KnowledgeDocument.owner_user_id == user_id,
            )
        )
    )
    # 分类与阈值按需动态拼接，避免 ``:param IS NULL OR ...`` 的参数绑定歧义（设计文档 §8.3）。
    if categories:
        stmt = stmt.where(KnowledgeDocument.category.in_(categories))
    if threshold > 0:
        stmt = stmt.where(score >= threshold)
    # ``<=>`` 升序（越近越靠前）才能命中 HNSW 索引；最后只取 top_k 条。
    stmt = stmt.order_by(distance.asc()).limit(top_k)

    rows = (await db.execute(stmt)).mappings().all()
    return [
        SearchHit(
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            chunk_index=row["chunk_index"],
            content=row["content"],
            heading=_heading(row["metadata"], row["document_title"]),
            document_title=row["document_title"],
            document_filename=row["document_filename"],
            category=row["category"],
            updated_at=row["updated_at"],
            score=round(float(row["score"]), 4),
        )
        for row in rows
    ]


__all__ = ["SearchHit", "search_chunks"]
