"""知识库编排层：入库、列表、详情、删除、重试、检索与重建。

分层约定（控制器 R3）：

- **请求级错误**（超出大小上限、扩展名不支持）必须抛给调用方
  （:class:`PayloadTooLargeError` / :class:`ValidationFailedError`），此时不建行、不落盘；
- **内容级错误**（解析、切分、向量化、原文读取失败）在 :func:`ingest_bytes` 等入口内捕获，
  落成 ``status="failed"`` + 中文 ``error``（截断 500 字符）后正常返回文档，供前端展示与重试。

首次入库、``retry_document`` 与 ``rebuild`` 共用 :func:`_index_document`，保证三条路径
的解析/切分/向量化行为完全一致；向量输入统一为 ``f"{heading}\\n{content}"``。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.cursor import encode_cursor
from app.core.errors import (
    ForbiddenError,
    NotFoundError,
    PayloadTooLargeError,
    ServiceUnavailableError,
    ValidationFailedError,
)
from app.core.storage import ObjectNotFoundError, get_storage
from app.db.models.knowledge import KnowledgeDocument
from app.db.models.user import User
from app.db.vector import SearchHit, search_chunks
from app.modules.knowledge import repository
from app.modules.knowledge.embedding import (
    EmbeddingUnavailableError,
    FakeEmbeddingProvider,
    get_embedding_provider,
)
from app.modules.knowledge.parsing import parse_document
from app.modules.knowledge.repository import visible_filter
from app.modules.knowledge.splitting import Chunk, split_text

logger = logging.getLogger(__name__)

_STORAGE_PREFIX = "knowledge/"
_MAX_ERROR_LENGTH = 500
# 与 parsing._SUPPORTED_SUFFIXES 保持一致：service 在落库/落盘之前先做请求级校验，
# 避免先建行写对象、再因为扩展名被拒绝而留下孤儿数据。
_SUPPORTED_SUFFIXES = (".md", ".markdown", ".txt", ".pdf", ".csv", ".json")
_DOCUMENT_STATUSES = frozenset({"pending", "indexing", "ready", "failed"})
_REBUILD_SCOPES = frozenset({"all", "system", "own"})
_MISSING_OBJECT_ERROR = "原始文件缺失，请重新上传"
_GENERIC_FAILURE_ERROR = "文档处理失败，请检查文件内容或稍后重试"


# --------------------------------------------------------------------- 公共工具
def _clip(message: str) -> str:
    """错误文案统一截断到 500 字符（spec §5.1 的列宽约定）。"""
    return (message or "").strip()[:_MAX_ERROR_LENGTH]


def _failure_message(exc: Exception) -> str:
    """把内容级异常转成用户可见的中文文案。

    ``ValueError``（解析/切分）与两个领域异常（embedding 不可用、对象缺失）的文案
    按约定本身就是中文，可直接展示；其余底层异常（``RecursionError``、pypdf 的英文
    异常等）绝不透出原文，统一中文兜底并记日志。
    """
    if isinstance(exc, (ValueError, EmbeddingUnavailableError, ObjectNotFoundError)):
        return _clip(str(exc)) or _GENERIC_FAILURE_ERROR
    logger.exception("知识库文档处理出现未预期异常")
    return _GENERIC_FAILURE_ERROR


def _storage_key(document_id: uuid.UUID, filename: str) -> str:
    """后端无关的存储键：``knowledge/<document_id><ext>``，扩展名小写。"""
    return f"{_STORAGE_PREFIX}{document_id}{Path(filename).suffix.lower()}"


def _embedding_text(chunk: Chunk) -> str:
    """向量输入必须包含章节标题；否则只出现在标题里的词对向量零贡献。"""
    return f"{chunk.heading}\n{chunk.content}" if chunk.heading else chunk.content


def _estimate_tokens(text: str) -> int:
    """保守 token 估算，与 ``app/agents/prompts._estimate_tokens`` 同口径。"""
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    return cjk + max(0, int((len(text) - cjk) * 0.3))


def _validate_upload(filename: str, raw: bytes, max_bytes: int) -> None:
    """请求级校验：扩展名与大小；失败直接抛 4xx，不产生任何副作用。"""
    suffix = Path(filename).suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        supported = "、".join(_SUPPORTED_SUFFIXES)
        raise ValidationFailedError(
            f"不支持的文件类型：{suffix or '（无扩展名）'}；仅支持 {supported}"
        )
    if len(raw) > max_bytes:
        raise PayloadTooLargeError(f"上传文件超过大小上限（{max_bytes} 字节）")


# ------------------------------------------------------------------ 失败状态落库
async def _commit_and_refresh(db: AsyncSession, document: KnowledgeDocument) -> KnowledgeDocument:
    """提交并刷新 ORM 对象。

    ``updated_at`` 由数据库 ``onupdate=func.now()`` 生成，UPDATE 提交后该属性处于
    过期状态；不刷新就序列化会触发同步懒加载（async 会话直接 MissingGreenlet）。
    """
    await db.commit()
    await db.refresh(document)
    return document


async def _commit_failed(
    db: AsyncSession, document: KnowledgeDocument, message: str
) -> KnowledgeDocument:
    """把文档置为 failed 并提交；数据库事务被污染时回滚后重建失败记录。"""
    message = _clip(message)
    # 先把字段快照下来：回滚会让持久化对象过期，甚至让未提交的新行被移除。
    snapshot: dict[str, Any] = {
        "id": document.id,
        "owner_user_id": document.owner_user_id,
        "title": document.title,
        "filename": document.filename,
        "source_uri": document.source_uri,
        "category": document.category,
        "checksum": document.checksum,
        "size_bytes": document.size_bytes,
        "storage_key": document.storage_key,
        "metadata_": dict(document.metadata_ or {}),
    }
    document.status = "failed"
    document.error = message
    document.chunk_count = 0
    try:
        await db.commit()
    except Exception:
        logger.exception("知识库失败状态提交失败，回滚后重试写入失败记录")
        await db.rollback()
    else:
        await db.refresh(document)
        return document

    existing = await db.get(KnowledgeDocument, snapshot["id"])
    if existing is None:
        existing = KnowledgeDocument(**snapshot, status="failed", error=message, chunk_count=0)
        db.add(existing)
    else:
        existing.status = "failed"
        existing.error = message
        existing.chunk_count = 0
    return await _commit_and_refresh(db, existing)


# --------------------------------------------------------------- 单文档索引流水线
async def _index_document(db: AsyncSession, document: KnowledgeDocument, raw: bytes) -> None:
    """解析 → 切分 → 向量化 → 批量替换分块，并把文档置为 ready（不提交）。"""
    settings = get_settings()
    # 先清掉上一轮分块：重试/重建不允许残留旧向量；失败时文档也不应留下半截数据。
    await repository.delete_chunks(db, document.id)
    document.chunk_count = 0

    parsed = parse_document(document.filename, raw, max_pdf_pages=settings.knowledge_max_pdf_pages)
    document.metadata_ = {**parsed.metadata, "characters": len(parsed.text)}
    chunks = split_text(
        parsed.text,
        size=settings.knowledge_chunk_size,
        overlap=settings.knowledge_chunk_overlap,
        max_chunks=settings.knowledge_max_chunks_per_document,
    )
    if not chunks:
        raise ValueError("文档切分后没有可入库的片段")

    vectors = await get_embedding_provider().embed([_embedding_text(chunk) for chunk in chunks])
    if len(vectors) != len(chunks):
        raise ValueError("Embedding 返回的向量数量与分块数量不一致")

    rows = [
        {
            "document_id": document.id,
            "chunk_index": chunk.index,
            "content": chunk.content,
            "embedding": vector,
            "token_count": _estimate_tokens(chunk.content),
            "metadata_": {**chunk.metadata, "char_start": chunk.char_start},
        }
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]
    await repository.insert_chunks(db, rows)
    document.status = "ready"
    document.chunk_count = len(chunks)
    document.error = None


# ----------------------------------------------------------------------- 入库
async def _ingest(
    db: AsyncSession,
    *,
    owner: User | None,
    title: str,
    filename: str,
    category: str,
    raw: bytes,
) -> tuple[KnowledgeDocument, bool]:
    """入库主流程；返回 ``(文档, 是否新建)``。

    ``created=False`` 表示 checksum 幂等命中已有文档（HTTP 层据此返回 200 而不是 201）。
    """
    settings = get_settings()
    _validate_upload(filename, raw, settings.knowledge_max_upload_bytes)

    owner_user_id = owner.id if owner is not None else None
    checksum = hashlib.sha256(raw).hexdigest()
    existing = await repository.find_by_checksum(db, owner_user_id=owner_user_id, checksum=checksum)
    if existing is not None:
        return existing, False

    document = KnowledgeDocument(
        owner_user_id=owner_user_id,
        title=title.strip() or Path(filename).stem or filename,
        filename=filename,
        source_uri=filename,
        category=(category or "").strip(),
        checksum=checksum,
        size_bytes=len(raw),
        status="indexing",
        storage_key="",
    )
    db.add(document)
    try:
        await db.flush()
    except IntegrityError:
        # 并发上传同内容：部分唯一索引兜底，回滚后返回已存在的那一行。
        await db.rollback()
        existing = await repository.find_by_checksum(
            db, owner_user_id=owner_user_id, checksum=checksum
        )
        if existing is not None:
            return existing, False
        raise

    document.storage_key = _storage_key(document.id, filename)
    try:
        await get_storage().put(document.storage_key, raw)
        await _index_document(db, document, raw)
    except Exception as exc:
        return await _commit_failed(db, document, _failure_message(exc)), True

    return await _commit_and_refresh(db, document), True


async def ingest_bytes(
    db: AsyncSession,
    *,
    owner: User | None,
    title: str,
    filename: str,
    category: str,
    raw: bytes,
) -> KnowledgeDocument:
    """入库一份文档字节；``owner=None`` 表示系统文档。

    - 同归属同 checksum 已存在时直接返回已有文档（幂等，不重复写文件）；
    - 内容级失败返回 ``status="failed"`` 的文档，不向调用方抛异常。
    """
    document, _created = await _ingest(
        db, owner=owner, title=title, filename=filename, category=category, raw=raw
    )
    return document


async def ingest_upload(
    db: AsyncSession,
    *,
    owner: User | None,
    title: str,
    filename: str,
    category: str,
    raw: bytes,
) -> tuple[KnowledgeDocument, bool]:
    """API 上传入口：与 :func:`ingest_bytes` 行为一致，额外返回是否为新建文档。"""
    return await _ingest(
        db, owner=owner, title=title, filename=filename, category=category, raw=raw
    )


# --------------------------------------------------------------------- 查询
async def _load_document(
    db: AsyncSession, user: User, document_id: uuid.UUID, *, write: bool
) -> KnowledgeDocument:
    """读取文档并做权限判定。

    - 他人私有文档一律 404（不泄露存在性）；
    - 系统文档所有人可读，但写操作（删除/重试）仅管理员；
    - 管理员可读写任意文档。
    """
    document = await repository.get_document(db, document_id)
    if document is None:
        raise NotFoundError("文档不存在")
    if user.role == "admin":
        return document
    if document.owner_user_id is None:
        if write:
            raise ForbiddenError("系统文档仅管理员可操作")
        return document
    if document.owner_user_id != user.id:
        raise NotFoundError("文档不存在")
    return document


async def get_document(db: AsyncSession, user: User, document_id: uuid.UUID) -> KnowledgeDocument:
    """按可见性读取文档详情。"""
    return await _load_document(db, user, document_id, write=False)


async def load_owners(
    db: AsyncSession, documents: Iterable[KnowledgeDocument]
) -> dict[uuid.UUID, User]:
    """批量读取文档归属人；系统文档（``owner_user_id`` 为空）不出现在结果中。

    API 层用一次 ``IN`` 查询组装 ``owner`` 字段，避免列表接口逐条取用户的 N+1。
    """
    owner_ids = {document.owner_user_id for document in documents if document.owner_user_id}
    if not owner_ids:
        return {}
    rows = await db.execute(select(User).where(User.id.in_(owner_ids)))
    return {owner.id: owner for owner in rows.scalars()}


async def list_documents(
    db: AsyncSession,
    user: User,
    *,
    q: str | None = None,
    category: str | None = None,
    status: str | None = None,
    owner_scope: Literal["visible", "all"] = "visible",
    limit: int = 20,
    cursor: str | None = None,
) -> tuple[list[KnowledgeDocument], str | None, bool, dict[str, int]]:
    """列表 + 游标分页 + 可见范围全量统计。"""
    if owner_scope not in {"visible", "all"}:
        raise ValidationFailedError("无效的归属范围")
    if owner_scope == "all" and user.role != "admin":
        raise ForbiddenError("仅管理员可查看全部文档")
    if status is not None and status not in _DOCUMENT_STATUSES:
        raise ValidationFailedError("无效的文档状态")
    if q is not None and len(q) > 100:
        raise ValidationFailedError("搜索关键词长度不能超过 100 个字符")
    limit = max(1, min(int(limit), 100))

    items, has_more = await repository.list_documents(
        db,
        user=user,
        q=q,
        category=category,
        status=status,
        owner_scope=owner_scope,
        limit=limit,
        cursor=cursor,
    )
    next_cursor: str | None = None
    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor({"t": last.updated_at, "id": last.id})
    stats = await repository.document_stats(db, user=user, owner_scope=owner_scope)
    return items, next_cursor, has_more, stats


# --------------------------------------------------------------------- 删除
async def delete_document(db: AsyncSession, user: User, document_id: uuid.UUID) -> None:
    """删除文档（分块随外键级联），提交后再删原文；原文删除失败只记日志。"""
    document = await _load_document(db, user, document_id, write=True)
    storage_key = document.storage_key
    await db.delete(document)
    await db.commit()
    if not storage_key:
        return
    try:
        await get_storage().delete(storage_key)
    except Exception:
        # 孤儿对象由后续清理任务处理，删除接口本身不应失败。
        logger.warning("知识库原文删除失败，留下孤儿对象 key=%s", storage_key, exc_info=True)


# --------------------------------------------------------------------- 重试
async def retry_document(db: AsyncSession, user: User, document_id: uuid.UUID) -> KnowledgeDocument:
    """从 storage_key 重读原文并重新入库；原文缺失/内容失败都落为 failed 文档。"""
    document = await _load_document(db, user, document_id, write=True)
    document.status = "indexing"
    document.chunk_count = 0
    # 先清旧分块：原文缺失时旧向量也不能残留（文档已不可检索，但不应占着库）。
    try:
        await repository.delete_chunks(db, document.id)
        raw = await get_storage().get(document.storage_key)
    except ObjectNotFoundError:
        return await _commit_failed(db, document, _MISSING_OBJECT_ERROR)
    except Exception as exc:
        return await _commit_failed(db, document, _failure_message(exc))

    try:
        await _index_document(db, document, raw)
    except Exception as exc:
        return await _commit_failed(db, document, _failure_message(exc))

    return await _commit_and_refresh(db, document)


# --------------------------------------------------------------------- 检索
async def search(
    db: AsyncSession,
    user: User,
    *,
    query: str,
    top_k: int | None = None,
    categories: list[str] | None = None,
) -> list[SearchHit]:
    """语义检索；范围恒为「系统 + 自己」，fake provider 下阈值降为 0。

    Embedding 服务不可用（真实 provider 网络/鉴权失败）映射为
    :class:`ServiceUnavailableError`，由 HTTP 层返回 503，而不是 500。
    """
    text = (query or "").strip()
    if not text:
        raise ValidationFailedError("检索内容不能为空")
    if len(text) > 500:
        raise ValidationFailedError("检索内容长度不能超过 500 个字符")

    settings = get_settings()
    effective_top_k = settings.vector_top_k if top_k is None else max(1, min(int(top_k), 20))
    provider = get_embedding_provider()
    try:
        vectors = await provider.embed([text])
    except EmbeddingUnavailableError as exc:
        raise ServiceUnavailableError(str(exc) or "Embedding 服务不可用") from exc
    if len(vectors) != 1:
        raise ServiceUnavailableError("Embedding 服务返回的向量条数异常")
    # fake 分数不可解释（短 query 对长 chunk 余弦存在上界），只按排序取 top_k。
    threshold = (
        0.0 if isinstance(provider, FakeEmbeddingProvider) else settings.vector_score_threshold
    )
    return await search_chunks(
        db,
        embedding=vectors[0],
        user_id=user.id,
        top_k=effective_top_k,
        threshold=threshold,
        categories=categories,
    )


# --------------------------------------------------------------------- 重建
async def rebuild(db: AsyncSession, *, scope: Literal["all", "system", "own"]) -> dict[str, int]:
    """按 scope 从原文重建索引，返回 ``documents/chunks/failed`` 统计。

    - ``documents``：本次处理的文档数（含失败）；
    - ``chunks``：重建成功后的分块总数；
    - ``failed``：原文缺失或重新处理失败、已置 ``status="failed"`` 的文档数。

    管理员校验由 router 负责；同一文档用行锁串行化，避免并发重建留下半截分块。
    """
    if scope not in _REBUILD_SCOPES:
        raise ValidationFailedError("无效的重建范围")
    documents = await repository.documents_for_rebuild(db, scope=scope)
    total_chunks = 0
    failed = 0
    for candidate in documents:
        # 行锁保证两个并发重建按顺序处理同一文档；锁定后读到的是最新已提交状态。
        document = await repository.lock_document(db, candidate.id)
        if document is None:
            continue
        document.status = "indexing"
        document.chunk_count = 0
        try:
            # 与 retry 一致：先清旧分块，原文缺失/失败时不留半截数据。
            await repository.delete_chunks(db, document.id)
            raw = await get_storage().get(document.storage_key)
        except ObjectNotFoundError:
            await _commit_failed(db, document, _MISSING_OBJECT_ERROR)
            failed += 1
            continue
        except Exception as exc:
            await _commit_failed(db, document, _failure_message(exc))
            failed += 1
            continue

        try:
            await _index_document(db, document, raw)
        except Exception as exc:
            await _commit_failed(db, document, _failure_message(exc))
            failed += 1
            continue

        chunk_count = document.chunk_count
        await db.commit()
        total_chunks += chunk_count

    return {"documents": len(documents), "chunks": total_chunks, "failed": failed}


__all__ = [
    "delete_document",
    "get_document",
    "ingest_bytes",
    "ingest_upload",
    "list_documents",
    "load_owners",
    "rebuild",
    "retry_document",
    "search",
    "visible_filter",
]
