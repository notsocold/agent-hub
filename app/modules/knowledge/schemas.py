"""知识库模块 DTO。

字段集合与前端冻结契约**完全一致**（spec §7.1 / §7.4）：文档对象 11 个字段、
检索命中 10 个字段，不含 ``filename`` / ``storage_key`` 等内部字段。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

KnowledgeDocumentStatus = Literal["pending", "indexing", "ready", "failed"]


class KnowledgeOwnerOut(BaseModel):
    """文档归属人摘要；系统文档在文档对象里表现为 ``owner=null``。"""

    id: uuid.UUID
    display_name: str


class KnowledgeDocumentOut(BaseModel):
    """知识库文档对象（11 字段，对齐前端 ``KnowledgeDocument``）。"""

    id: uuid.UUID
    title: str
    source_uri: str
    category: str
    status: KnowledgeDocumentStatus
    chunk_count: int
    size_bytes: int
    owner: KnowledgeOwnerOut | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class KnowledgeStats(BaseModel):
    """列表统计（6 个键，可见范围内全量，不受 q/category/status 过滤影响）。"""

    documents: int
    chunks: int
    ready: int
    indexing: int
    pending: int
    failed: int


class KnowledgeDocumentPage(BaseModel):
    """文档列表：游标分页 + 可见范围全量统计。"""

    items: list[KnowledgeDocumentOut]
    next_cursor: str | None
    has_more: bool
    # 6 个键：documents/chunks/ready/indexing/pending/failed。
    stats: KnowledgeStats


class KnowledgeSearchHitOut(BaseModel):
    """单条检索命中（10 字段，对齐前端 ``KnowledgeSearchHit``）。"""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    # title 是分块所在章节标题（SearchHit.heading），document_title 是文档标题。
    title: str
    document_title: str
    document_filename: str
    category: str
    chunk_index: int
    content: str
    score: float
    updated_at: datetime


class KnowledgeSearchFilters(BaseModel):
    category: str | None = None


class KnowledgeSearchRequest(BaseModel):
    """检索请求；范围与长度限制由 service 再兜底一次。"""

    query: str = Field(min_length=1, max_length=500)
    top_k: int | None = Field(default=None, ge=1, le=20)
    filters: KnowledgeSearchFilters | None = None


class KnowledgeSearchOut(BaseModel):
    hits: list[KnowledgeSearchHitOut]
    elapsed_ms: int


class KnowledgeRebuildRequest(BaseModel):
    scope: Literal["all", "system", "own"]


class KnowledgeRebuildOut(BaseModel):
    # R10 口径：documents 为处理总数、failed 为其中失败数、chunks 为成功重建的分块总数。
    documents: int
    chunks: int
    failed: int
    elapsed_ms: int


__all__ = [
    "KnowledgeDocumentOut",
    "KnowledgeDocumentPage",
    "KnowledgeDocumentStatus",
    "KnowledgeOwnerOut",
    "KnowledgeRebuildOut",
    "KnowledgeRebuildRequest",
    "KnowledgeSearchFilters",
    "KnowledgeSearchHitOut",
    "KnowledgeSearchOut",
    "KnowledgeSearchRequest",
    "KnowledgeStats",
]
