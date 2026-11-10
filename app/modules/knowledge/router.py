"""知识库路由：7 个 HTTP 端点，只做参数校验与 HTTP 映射。

权限判定（本人/管理员）、可见性过滤、统计口径、幂等与失败分层全部在
:mod:`app.modules.knowledge.service`；本文件不重复业务逻辑。
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import AdminUser, CurrentUser, DbSession
from app.core.errors import CapabilityDisabledError, ForbiddenError, PayloadTooLargeError
from app.db.models.knowledge import KnowledgeDocument
from app.db.models.user import User
from app.db.vector import SearchHit
from app.modules.knowledge import service
from app.modules.knowledge.schemas import (
    KnowledgeDocumentOut,
    KnowledgeDocumentPage,
    KnowledgeOwnerOut,
    KnowledgeRebuildOut,
    KnowledgeRebuildRequest,
    KnowledgeSearchHitOut,
    KnowledgeSearchOut,
    KnowledgeSearchRequest,
)

TitleForm = Annotated[str | None, Form(max_length=200)]
CategoryForm = Annotated[str | None, Form(max_length=64)]
ScopeForm = Annotated[Literal["user", "system"], Form()]


async def require_knowledge_enabled() -> None:
    """知识库能力总开关（spec §7.6）：关闭时所有端点统一 403。"""
    if not get_settings().knowledge_enabled:
        raise CapabilityDisabledError("知识库能力未启用")


# 门禁放在 router 级依赖上，7 个端点一次生效，避免逐端点重复检查。
router = APIRouter(
    prefix="/knowledge",
    tags=["知识库"],
    dependencies=[Depends(require_knowledge_enabled)],
)


# ------------------------------------------------------------------ ORM → DTO 映射
def _owner_out(owner: User | None) -> KnowledgeOwnerOut | None:
    return KnowledgeOwnerOut(id=owner.id, display_name=owner.display_name) if owner else None


def _document_out(document: KnowledgeDocument, owner: User | None) -> KnowledgeDocumentOut:
    return KnowledgeDocumentOut(
        id=document.id,
        title=document.title,
        source_uri=document.source_uri,
        category=document.category,
        status=document.status,  # type: ignore[arg-type]
        chunk_count=document.chunk_count,
        size_bytes=document.size_bytes,
        owner=_owner_out(owner),
        # error 为 None 表示无错误，绝不返回空字符串（spec §7.1）。
        error=document.error or None,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _hit_out(hit: SearchHit) -> KnowledgeSearchHitOut:
    return KnowledgeSearchHitOut(
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        # 前端 title = 分块章节标题（heading），document_title = 文档标题。
        title=hit.heading,
        document_title=hit.document_title,
        document_filename=hit.document_filename,
        category=hit.category,
        chunk_index=hit.chunk_index,
        content=hit.content,
        score=hit.score,
        updated_at=hit.updated_at,
    )


async def _document_payload(db: AsyncSession, document: KnowledgeDocument) -> KnowledgeDocumentOut:
    owners = await service.load_owners(db, [document])
    return _document_out(document, owners.get(document.owner_user_id))


def _elapsed_ms(started: float) -> int:
    # round() 不传 ndigits 时返回 int，直接作为毫秒数。
    return round((time.perf_counter() - started) * 1000)


# ------------------------------------------------------------------------ 上传
@router.post(
    "/documents",
    response_model=KnowledgeDocumentOut,
    status_code=201,
    summary="上传知识库文档",
)
async def upload_document(
    response: Response,
    user: CurrentUser,
    db: DbSession,
    file: UploadFile,
    title: TitleForm = None,
    category: CategoryForm = None,
    scope: ScopeForm = "user",
) -> KnowledgeDocumentOut:
    """multipart 上传并同步入库，返回终态文档。

    - 新建返回 **201**；同归属同 checksum 已存在时返回 **200 + 已有文档**（幂等）；
    - 超过 ``KNOWLEDGE_MAX_UPLOAD_BYTES`` → 413 ``payload_too_large``；
    - 扩展名不支持 → 422 ``validation_error``；
    - 解析/向量化等内容级失败仍返回 **201**，文档为 ``status=failed`` + 中文 ``error``；
    - ``scope=system`` 仅管理员可用，普通用户 → 403 ``forbidden``。
    """
    if scope == "system" and user.role != "admin":
        raise ForbiddenError("系统文档仅管理员可上传")

    raw = await file.read()
    max_bytes = get_settings().knowledge_max_upload_bytes
    if len(raw) > max_bytes:
        raise PayloadTooLargeError(f"上传文件超过大小上限（{max_bytes} 字节）")

    document, created = await service.ingest_upload(
        db,
        owner=None if scope == "system" else user,
        title=title or "",
        filename=file.filename or "",
        category=category or "",
        raw=raw,
    )
    if not created:
        # checksum 幂等命中：200 而非 201（spec §7.3，不引入 duplicate 字段污染文档对象）。
        response.status_code = 200
    return await _document_payload(db, document)


# ------------------------------------------------------------------------ 列表
@router.get("/documents", response_model=KnowledgeDocumentPage, summary="知识库文档列表")
async def list_knowledge_documents(
    user: CurrentUser,
    db: DbSession,
    q: str | None = Query(default=None, min_length=1, max_length=100),
    category: str | None = None,
    status: Literal["pending", "indexing", "ready", "failed"] | None = None,
    owner_scope: Literal["visible", "all"] = "visible",
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
) -> KnowledgeDocumentPage:
    """按可见范围分页列出文档，并返回 6 键全量统计。

    - ``owner_scope=visible``（默认）只看「系统 + 自己」；``all`` 仅管理员，
      普通用户 → 403 ``forbidden``；
    - ``stats`` 统计可见范围内全量文档，不受 ``q`` / ``category`` / ``status`` 影响；
    - 排序 ``updated_at DESC, id DESC``，游标见 ``next_cursor``。
    """
    items, next_cursor, has_more, stats = await service.list_documents(
        db,
        user,
        q=q,
        category=category,
        status=status,
        owner_scope=owner_scope,
        limit=limit,
        cursor=cursor,
    )
    owners = await service.load_owners(db, items)
    return KnowledgeDocumentPage(
        items=[_document_out(item, owners.get(item.owner_user_id)) for item in items],
        next_cursor=next_cursor,
        has_more=has_more,
        stats=stats,
    )


# ------------------------------------------------------------------------ 详情
@router.get(
    "/documents/{document_id}",
    response_model=KnowledgeDocumentOut,
    summary="知识库文档详情",
)
async def get_knowledge_document(
    document_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> KnowledgeDocumentOut:
    """读取文档详情；不存在或属于他人的私有文档统一 404（不泄露存在性）。"""
    document = await service.get_document(db, user, document_id)
    return await _document_payload(db, document)


# ------------------------------------------------------------------------ 删除
@router.delete("/documents/{document_id}", status_code=204, summary="删除知识库文档")
async def delete_knowledge_document(
    document_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> None:
    """删除文档、分块与原始文件；可见性规则与详情一致（他人私有文档 404）。"""
    await service.delete_document(db, user, document_id)


# ------------------------------------------------------------------------ 重试
@router.post(
    "/documents/{document_id}/retry",
    response_model=KnowledgeDocumentOut,
    summary="重试文档入库",
)
async def retry_knowledge_document(
    document_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> KnowledgeDocumentOut:
    """从原始文件重新解析入库，返回最新文档对象；原文缺失时文档置 failed。"""
    document = await service.retry_document(db, user, document_id)
    return await _document_payload(db, document)


# ------------------------------------------------------------------------ 检索
@router.post("/search", response_model=KnowledgeSearchOut, summary="知识库语义检索")
async def search_knowledge(
    payload: KnowledgeSearchRequest, user: CurrentUser, db: DbSession
) -> KnowledgeSearchOut:
    """语义检索自己的文档与系统文档（管理员也不例外，避免混入他人私有资料）。

    - ``query`` 长度 1–500，``top_k`` 范围 1–20（缺省取 ``VECTOR_TOP_K``）；
    - ``filters.category`` 非空时只检索该分类；
    - 命中字段对齐前端 ``KnowledgeSearchHit``（10 个）；``title`` 取分块章节标题；
    - 库为空返回空 ``hits``（不是错误）；``elapsed_ms`` 为本次检索耗时；
    - Embedding 服务不可用 → 503 ``service_unavailable``。
    """
    category = (payload.filters.category or "").strip() if payload.filters else ""
    started = time.perf_counter()
    hits = await service.search(
        db,
        user,
        query=payload.query,
        top_k=payload.top_k,
        categories=[category] if category else None,
    )
    return KnowledgeSearchOut(
        hits=[_hit_out(hit) for hit in hits],
        elapsed_ms=_elapsed_ms(started),
    )


# ------------------------------------------------------------------------ 重建
@router.post("/rebuild", response_model=KnowledgeRebuildOut, summary="重建知识库索引")
async def rebuild_knowledge_index(
    payload: KnowledgeRebuildRequest, admin: AdminUser, db: DbSession
) -> KnowledgeRebuildOut:
    """仅管理员：按 scope 从原始文件全量重建索引。

    - ``scope="own"`` 指**所有用户的私有文档**，不是管理员本人的文档（控制器 R9）；
    - 返回口径（控制器 R10）：``documents`` 为本次处理的文档总数（含失败），
      ``failed`` 为其中失败的条数，``chunks`` 为重建成功后成功写入的分块总数；
    - 原文缺失的文档计入 ``failed``，置 ``status=failed`` 并写中文 ``error``。
    """
    started = time.perf_counter()
    result = await service.rebuild(db, scope=payload.scope)
    return KnowledgeRebuildOut(**result, elapsed_ms=_elapsed_ms(started))


__all__ = ["require_knowledge_enabled", "router"]
