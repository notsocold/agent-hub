"""M4 知识库 service 层集成测试：入库、幂等、失败分层、重试、删除、列表、检索、重建。

用例全部使用真实数据库行与真实落盘文件（``data/test-knowledge``），向量用
确定性 :class:`FakeEmbeddingProvider`；文件名/标题等业务文案断言为中文。
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import reset_settings_cache
from app.core.errors import (
    ForbiddenError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationFailedError,
)
from app.core.storage import LocalFileStorage
from app.db.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.db.models.user import User
from app.db.session import get_session_factory
from app.modules.knowledge import repository
from app.modules.knowledge.embedding import EmbeddingUnavailableError
from app.modules.knowledge.service import (
    delete_document,
    get_document,
    ingest_bytes,
    list_documents,
    rebuild,
    retry_document,
    search,
)

STORAGE_ROOT = Path("data/test-knowledge")


# --------------------------------------------------------------------- 测试辅助
async def _create_user(*, role: str = "user") -> User:
    """测试文件私有的第二用户，用于归属隔离场景。"""
    async with get_session_factory()() as session:
        row = User(
            email=f"{role}-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="x",
            display_name="其他用户",
            role=role,
        )
        session.add(row)
        await session.commit()
        return row


async def _ingest(
    db: AsyncSession,
    owner: User | None,
    *,
    raw: bytes = "# 标题\n\n正文内容。".encode(),
    title: str = "测试文档",
    filename: str = "doc.md",
    category: str = "通用",
) -> KnowledgeDocument:
    return await ingest_bytes(
        db, owner=owner, title=title, filename=filename, category=category, raw=raw
    )


async def _chunks_of(db: AsyncSession, document_id: uuid.UUID) -> list[KnowledgeChunk]:
    rows = await db.execute(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.document_id == document_id)
        .order_by(KnowledgeChunk.chunk_index)
    )
    return list(rows.scalars())


async def _document_count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(KnowledgeDocument)) or 0)


def _stored_files() -> list[Path]:
    return [path for path in STORAGE_ROOT.rglob("*") if path.is_file()]


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


class _BrokenEmbeddingProvider:
    """向量化失败注入：模拟真实 provider 不可用。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailableError("Embedding 服务不可用")


@pytest.fixture
async def other_user(db: AsyncSession) -> User:
    """另一位普通用户（本文件私有，避免污染 conftest）。"""
    return await _create_user()


# ------------------------------------------------------------------- 入库成功路径
async def test_ingest_creates_ready_document(
    db: AsyncSession, user: User, fake_storage: LocalFileStorage, fake_embedding: Any
) -> None:
    raw = "# 吊牌\n\n退货要保留吊牌。".encode()
    document = await _ingest(db, user, raw=raw, title="退货政策", filename="r.md", category="售后")

    assert document.status == "ready"
    assert document.chunk_count >= 1
    assert document.error is None
    assert document.size_bytes == len(raw)
    assert document.checksum == hashlib.sha256(raw).hexdigest()
    assert document.source_uri == "r.md"
    assert document.storage_key == f"knowledge/{document.id}.md"
    assert document.metadata_["characters"] == len(raw.decode())
    assert await fake_storage.exists(document.storage_key)

    chunks = await _chunks_of(db, document.id)
    assert len(chunks) == document.chunk_count
    assert chunks[0].metadata_["heading"] == "吊牌"
    # char_start 是正文（不含标题行）在原文中的偏移。
    assert chunks[0].metadata_["char_start"] == raw.decode().index("退货")
    assert chunks[0].token_count > 0


async def test_ingest_uses_filename_stem_when_title_blank(
    db: AsyncSession, user: User, fake_storage: LocalFileStorage, fake_embedding: Any
) -> None:
    document = await _ingest(db, user, title="   ", filename="return-policy.md")
    assert document.title == "return-policy"
    assert document.storage_key.endswith(".md")


async def test_ingest_duplicate_returns_existing_and_writes_once(
    db: AsyncSession, user: User, fake_storage: LocalFileStorage, fake_embedding: Any
) -> None:
    raw = "# 同一份\n\n完全相同的字节。".encode()
    first = await _ingest(db, user, raw=raw)
    second = await _ingest(db, user, raw=raw)

    assert first.id == second.id
    assert await _document_count(db) == 1
    assert len(_stored_files()) == 1


async def test_ingest_duplicate_scoped_by_owner(
    db: AsyncSession,
    user: User,
    other_user: User,
    fake_storage: LocalFileStorage,
    fake_embedding: Any,
) -> None:
    raw = "# 同内容\n\n两个用户各存一份。".encode()
    mine = await _ingest(db, user, raw=raw, title="我的")
    theirs = await _ingest(db, other_user, raw=raw, title="他的")

    assert mine.id != theirs.id
    assert await _document_count(db) == 2
    assert len(_stored_files()) == 2


async def test_ingest_system_document_is_visible_to_users(
    db: AsyncSession, user: User, fake_storage: LocalFileStorage, fake_embedding: Any
) -> None:
    document = await _ingest(db, None, title="平台公告", filename="notice.md")

    loaded = await get_document(db, user, document.id)
    assert loaded.id == document.id
    items, _, _, stats = await list_documents(db, user)
    assert document.id in {item.id for item in items}
    assert stats["documents"] == 1


# ----------------------------------------------------------------- 请求级错误（抛出）
async def test_ingest_rejects_unsupported_extension(
    db: AsyncSession, user: User, fake_storage: LocalFileStorage, fake_embedding: Any
) -> None:
    with pytest.raises(ValidationFailedError) as excinfo:
        await _ingest(db, user, filename="bad.exe", raw=b"x")

    assert _has_cjk(excinfo.value.message)
    assert "不支持" in excinfo.value.message
    # 请求级错误必须在落库/落盘之前拦下，不留下半截文档与孤儿对象。
    assert await _document_count(db) == 0
    assert _stored_files() == []


async def test_ingest_rejects_oversize_upload(
    db: AsyncSession, user: User, fake_storage: LocalFileStorage, fake_embedding: Any, monkeypatch
) -> None:
    monkeypatch.setenv("KNOWLEDGE_MAX_UPLOAD_BYTES", "10")
    reset_settings_cache()
    try:
        with pytest.raises(PayloadTooLargeError):
            await _ingest(db, user, raw=b"x" * 11)
    finally:
        reset_settings_cache()

    assert await _document_count(db) == 0
    assert _stored_files() == []


# ----------------------------------------------------------------- 内容级失败（failed）
async def test_ingest_parse_failure_marks_failed_document(
    db: AsyncSession, user: User, fake_storage: LocalFileStorage, fake_embedding: Any
) -> None:
    document = await _ingest(db, user, filename="broken.md", raw=b"\xff\xfe\xfd")

    assert document.status == "failed"
    assert document.chunk_count == 0
    assert document.error and "UTF-8" in document.error
    # 原文已落盘，后续可重试；分块不残留。
    assert await fake_storage.exists(document.storage_key)
    assert await _chunks_of(db, document.id) == []


async def test_ingest_embedding_failure_marks_failed_document(
    db: AsyncSession,
    user: User,
    fake_storage: LocalFileStorage,
    fake_embedding: Any,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.modules.knowledge.service.get_embedding_provider",
        lambda: _BrokenEmbeddingProvider(),
    )
    document = await _ingest(db, user)

    assert document.status == "failed"
    assert document.error == "Embedding 服务不可用"
    assert document.chunk_count == 0
    assert await _chunks_of(db, document.id) == []
    assert await fake_storage.exists(document.storage_key)


async def test_ingest_non_value_error_message_is_chinese(
    db: AsyncSession, user: User, fake_storage: LocalFileStorage, fake_embedding: Any
) -> None:
    # 超深嵌套 JSON 在标准库 json.loads 阶段抛 RecursionError（英文消息）。
    raw = b"[" * 3000 + b"0" + b"]" * 3000
    document = await _ingest(db, user, filename="deep.json", raw=raw)

    assert document.status == "failed"
    assert document.error and _has_cjk(document.error)
    assert "recursion" not in document.error.lower()
    assert "maximum" not in document.error.lower()


async def test_ingest_split_failure_truncates_error_to_500(
    db: AsyncSession, user: User, fake_storage: LocalFileStorage, fake_embedding: Any, monkeypatch
) -> None:
    def _boom(*args: Any, **kwargs: Any) -> None:
        raise ValueError("切分" * 300)

    monkeypatch.setattr("app.modules.knowledge.service.split_text", _boom)
    document = await _ingest(db, user)

    assert document.status == "failed"
    assert document.error is not None
    assert len(document.error) == 500


# ------------------------------------------------------- embedding 输入携带章节标题
async def test_embedding_input_includes_heading(
    db: AsyncSession, user: User, fake_storage: LocalFileStorage, fake_embedding: Any
) -> None:
    """同正文、不同章节标题必须产生不同向量（Task 6 承运要求）。"""
    text = "# 甲\n\n同一段正文内容。\n\n# 乙\n\n同一段正文内容。"
    document = await _ingest(db, user, raw=text.encode())

    chunks = await _chunks_of(db, document.id)
    assert len(chunks) == 2
    assert chunks[0].content == chunks[1].content == "同一段正文内容。"
    assert chunks[0].metadata_["heading"] == "甲"
    assert chunks[1].metadata_["heading"] == "乙"
    assert chunks[0].embedding != chunks[1].embedding


# ------------------------------------------------------------------- 重试（retry）
async def test_retry_rereads_original_bytes_and_succeeds(
    db: AsyncSession, user: User, fake_storage: LocalFileStorage, fake_embedding: Any
) -> None:
    document = await _ingest(db, user, filename="broken.md", raw=b"\xff\xfe\xfd")
    assert document.status == "failed"

    # 修好存储里的原文件后重试：service 必须重新读 storage，而不是复用旧内容。
    fixed = "# 修复\n\n现在可以正常解析。".encode()
    await fake_storage.put(document.storage_key, fixed)
    retried = await retry_document(db, user, document.id)

    assert retried.id == document.id
    assert retried.status == "ready"
    assert retried.error is None
    assert retried.chunk_count >= 1
    assert len(await _chunks_of(db, document.id)) == retried.chunk_count


async def test_retry_acquires_document_row_lock(
    db: AsyncSession,
    user: User,
    fake_storage: LocalFileStorage,
    fake_embedding: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retry 开头必须与 rebuild 一样拿行锁，避免并发写同一文档。"""
    document = await _ingest(db, user, filename="lock.md")
    locked: list[uuid.UUID] = []
    original = repository.lock_document

    async def _spy_lock(
        session: AsyncSession, document_id: uuid.UUID
    ) -> KnowledgeDocument | None:
        locked.append(document_id)
        return await original(session, document_id)

    monkeypatch.setattr(repository, "lock_document", _spy_lock)

    retried = await retry_document(db, user, document.id)

    assert retried.id == document.id
    assert locked == [document.id]


async def test_retry_missing_object_marks_failed(
    db: AsyncSession, user: User, fake_storage: LocalFileStorage, fake_embedding: Any
) -> None:
    document = await _ingest(db, user)
    assert document.status == "ready"
    await fake_storage.delete(document.storage_key)

    retried = await retry_document(db, user, document.id)

    assert retried.status == "failed"
    assert retried.error is not None and "原始文件缺失" in retried.error
    assert retried.chunk_count == 0
    assert await _chunks_of(db, document.id) == []


async def test_retry_other_users_document_raises_not_found(
    db: AsyncSession, user: User, seeded_docs: dict[str, Any], fake_embedding: Any
) -> None:
    with pytest.raises(NotFoundError):
        await retry_document(db, user, seeded_docs["other"].id)


async def test_retry_system_document_forbidden_for_normal_user(
    db: AsyncSession, user: User, seeded_docs: dict[str, Any], fake_embedding: Any
) -> None:
    with pytest.raises(ForbiddenError):
        await retry_document(db, user, seeded_docs["system"].id)


# --------------------------------------------------------------------- 删除
async def test_delete_removes_chunks_and_storage_object(
    db: AsyncSession, user: User, fake_storage: LocalFileStorage, fake_embedding: Any
) -> None:
    document = await _ingest(db, user)
    assert len(await _chunks_of(db, document.id)) > 0
    storage_key = document.storage_key

    await delete_document(db, user, document.id)

    assert await db.get(KnowledgeDocument, document.id) is None
    remaining = await db.scalar(
        select(func.count())
        .select_from(KnowledgeChunk)
        .where(KnowledgeChunk.document_id == document.id)
    )
    assert remaining == 0
    assert await fake_storage.exists(storage_key) is False


async def test_delete_other_users_document_raises_not_found(
    db: AsyncSession, user: User, seeded_docs: dict[str, Any], fake_embedding: Any
) -> None:
    with pytest.raises(NotFoundError):
        await delete_document(db, user, seeded_docs["other"].id)

    assert await db.get(KnowledgeDocument, seeded_docs["other"].id) is not None


async def test_admin_can_delete_system_document(
    db: AsyncSession, admin: User, seeded_docs: dict[str, Any], fake_embedding: Any
) -> None:
    await delete_document(db, admin, seeded_docs["system"].id)
    assert await db.get(KnowledgeDocument, seeded_docs["system"].id) is None


# --------------------------------------------------------------- 读取与可见性
async def test_get_document_hides_other_users_private_document(
    db: AsyncSession, user: User, seeded_docs: dict[str, Any], fake_embedding: Any
) -> None:
    with pytest.raises(NotFoundError):
        await get_document(db, user, seeded_docs["other"].id)

    assert (await get_document(db, user, seeded_docs["system"].id)).id == seeded_docs["system"].id


async def test_list_documents_visible_scope_and_stats(
    db: AsyncSession, user: User, seeded_docs: dict[str, Any], fake_embedding: Any
) -> None:
    items, next_cursor, has_more, stats = await list_documents(db, user)

    assert {item.id for item in items} == {seeded_docs["system"].id, seeded_docs["own"].id}
    assert next_cursor is None
    assert has_more is False
    assert stats == {
        "documents": 2,
        "chunks": seeded_docs["system"].chunk_count + seeded_docs["own"].chunk_count,
        "ready": 2,
        "indexing": 0,
        "pending": 0,
        "failed": 0,
    }


async def test_list_documents_owner_scope_all_requires_admin(
    db: AsyncSession, user: User, admin: User, seeded_docs: dict[str, Any], fake_embedding: Any
) -> None:
    with pytest.raises(ForbiddenError):
        await list_documents(db, user, owner_scope="all")

    items, _, _, stats = await list_documents(db, admin, owner_scope="all")
    assert len(items) == 3
    assert stats["documents"] == 3


async def test_list_documents_filters_and_cursor_pagination(
    db: AsyncSession, admin: User, seeded_docs: dict[str, Any], fake_embedding: Any
) -> None:
    # 过滤不影响 stats（stats 统计可见范围全量）。
    items, _, _, stats = await list_documents(db, admin, owner_scope="all", q="退货")
    assert [item.id for item in items] == [seeded_docs["system"].id]
    assert stats["documents"] == 3

    items, _, _, _ = await list_documents(db, admin, owner_scope="all", category="物流")
    assert [item.id for item in items] == [seeded_docs["own"].id]

    items, _, _, _ = await list_documents(db, admin, owner_scope="all", status="failed")
    assert items == []

    seen: list[uuid.UUID] = []
    cursor: str | None = None
    for _ in range(5):
        page, cursor, has_more, _ = await list_documents(
            db, admin, owner_scope="all", limit=1, cursor=cursor
        )
        seen.extend(item.id for item in page)
        if not has_more:
            break
    assert len(seen) == 3
    assert len(set(seen)) == 3


async def test_visible_filter_hides_other_users_from_search(
    db: AsyncSession, user: User, admin: User, seeded_docs: dict[str, Any], fake_embedding: Any
) -> None:
    # fake 模式阈值 0，不相关分块也会以 0 分返回；可见性断言应看 document_id。
    hits = await search(db, user, query="私密暗号", top_k=5, categories=None)
    assert all(hit.document_id != seeded_docs["other"].id for hit in hits)

    system_hits = await search(db, user, query="保留吊牌", top_k=5, categories=None)
    assert any(hit.document_id == seeded_docs["system"].id for hit in system_hits)

    # 管理员检索范围也恒为「系统 + 自己」，不会借角色看到他人私有文档。
    admin_hits = await search(db, admin, query="私密暗号", top_k=5, categories=None)
    assert all(hit.document_id != seeded_docs["other"].id for hit in admin_hits)


# --------------------------------------------------------------------- 检索
async def test_search_uses_zero_threshold_for_fake_provider(
    db: AsyncSession, user: User, fake_storage: LocalFileStorage, fake_embedding: Any
) -> None:
    # 长 chunk 对短 query 的 fake 余弦约 0.2，低于默认阈值 0.30；
    # service 必须在 fake provider 下把阈值降为 0 才能召回（spec §8.2）。
    body = "本店支持七天无理由退货，请保留吊牌与完整包装。" * 8
    raw = f"# 退货\n\n{body}".encode()
    document = await _ingest(db, user, raw=raw, title="退货长文", category="售后")

    hits = await search(db, user, query="退货需要保留吊牌吗", top_k=3, categories=None)

    assert hits, "fake provider 下短 query 应能命中长 chunk（阈值必须为 0）"
    assert hits[0].document_id == document.id
    assert hits[0].content
    assert 0.0 <= hits[0].score <= 1.0


async def test_search_category_filter_and_top_k(
    db: AsyncSession, user: User, seeded_docs: dict[str, Any], fake_embedding: Any
) -> None:
    hits = await search(db, user, query="保留吊牌", top_k=1, categories=None)
    assert len(hits) == 1
    assert hits[0].document_id == seeded_docs["system"].id

    filtered = await search(db, user, query="保留吊牌", top_k=5, categories=["物流"])
    assert filtered
    assert all(hit.category == "物流" for hit in filtered)
    assert all(hit.document_id != seeded_docs["system"].id for hit in filtered)


# --------------------------------------------------------------------- 重建
async def test_rebuild_recreates_chunks(
    db: AsyncSession, admin: User, seeded_docs: dict[str, Any], fake_embedding: Any
) -> None:
    before = {row for row in (await db.execute(select(KnowledgeChunk.id))).scalars()}
    assert len(before) > 0

    result = await rebuild(db, scope="all")

    assert result["documents"] == 3
    assert result["failed"] == 0
    after = set((await db.execute(select(KnowledgeChunk.id))).scalars())
    assert after.isdisjoint(before)
    assert result["chunks"] == len(after)
    for seeded in seeded_docs["documents"]:
        document = await db.get(KnowledgeDocument, seeded.id)
        assert document is not None
        assert document.status == "ready"
        assert document.chunk_count == len(await _chunks_of(db, seeded.id))


async def test_rebuild_scope_system_leaves_user_documents_untouched(
    db: AsyncSession, admin: User, seeded_docs: dict[str, Any], fake_embedding: Any
) -> None:
    own_before = {
        row
        for row in (
            await db.execute(
                select(KnowledgeChunk.id).where(KnowledgeChunk.document_id == seeded_docs["own"].id)
            )
        ).scalars()
    }
    system_before = {
        row
        for row in (
            await db.execute(
                select(KnowledgeChunk.id).where(
                    KnowledgeChunk.document_id == seeded_docs["system"].id
                )
            )
        ).scalars()
    }

    result = await rebuild(db, scope="system")

    assert result["documents"] == 1
    assert result["failed"] == 0
    own_after = set(
        (
            await db.execute(
                select(KnowledgeChunk.id).where(KnowledgeChunk.document_id == seeded_docs["own"].id)
            )
        ).scalars()
    )
    system_after = set(
        (
            await db.execute(
                select(KnowledgeChunk.id).where(
                    KnowledgeChunk.document_id == seeded_docs["system"].id
                )
            )
        ).scalars()
    )
    assert own_after == own_before
    assert system_after.isdisjoint(system_before)


async def test_rebuild_scope_own_covers_all_user_documents(
    db: AsyncSession, admin: User, seeded_docs: dict[str, Any], fake_embedding: Any
) -> None:
    system_before = {
        row
        for row in (
            await db.execute(
                select(KnowledgeChunk.id).where(
                    KnowledgeChunk.document_id == seeded_docs["system"].id
                )
            )
        ).scalars()
    }

    result = await rebuild(db, scope="own")

    assert result["documents"] == 2
    system_after = set(
        (
            await db.execute(
                select(KnowledgeChunk.id).where(
                    KnowledgeChunk.document_id == seeded_docs["system"].id
                )
            )
        ).scalars()
    )
    assert system_after == system_before


async def test_rebuild_missing_object_counts_failed(
    db: AsyncSession,
    admin: User,
    seeded_docs: dict[str, Any],
    fake_storage: LocalFileStorage,
    fake_embedding: Any,
) -> None:
    system = seeded_docs["system"]
    await fake_storage.delete(system.storage_key)

    result = await rebuild(db, scope="all")

    assert result["documents"] == 3
    assert result["failed"] == 1
    document = await db.get(KnowledgeDocument, system.id)
    assert document is not None
    assert document.status == "failed"
    assert document.error is not None and "原始文件缺失" in document.error
    assert document.chunk_count == 0
    assert await _chunks_of(db, system.id) == []


async def test_rebuild_rejects_unknown_scope(db: AsyncSession, admin: User) -> None:
    with pytest.raises(ValidationFailedError):
        await rebuild(db, scope="everything")  # type: ignore[arg-type]


# ----------------------------------------------------------------- 并发幂等
async def test_concurrent_ingest_creates_single_row(
    db: AsyncSession, user: User, fake_storage: LocalFileStorage, fake_embedding: Any
) -> None:
    """三个并发上传同内容：唯一索引兜底，全部返回同一文档。"""
    raw = "# 并发\n\n同一份内容只允许一行。".encode()

    async def _one() -> KnowledgeDocument:
        async with get_session_factory()() as session:
            return await ingest_bytes(
                session, owner=user, title="并发", filename="race.md", category="通用", raw=raw
            )

    documents = await asyncio.gather(_one(), _one(), _one())

    assert len({document.id for document in documents}) == 1
    assert await _document_count(db) == 1
    assert len(_stored_files()) == 1
