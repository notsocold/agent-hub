"""M4 知识库数据模型测试：列定义、索引、唯一性分域与级联删除。

这些用例走 ``Base.metadata.create_all``（conftest 的 ``db`` 夹具），断言的是模型定义
真实落库后的结构；迁移 ``20261111_1000_*_m4_knowledge.py`` 必须与之保持一致。
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.db.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.db.models.user import User
from app.db.session import get_session_factory

# 列名 -> (udt_name, character_maximum_length, 是否可空)；精确相等，多一列少一列都会失败。
_DOCUMENT_COLUMNS: dict[str, tuple[str, int | None, bool]] = {
    "id": ("uuid", None, False),
    "owner_user_id": ("uuid", None, True),
    "title": ("varchar", 200, False),
    "filename": ("varchar", 255, False),
    "source_uri": ("text", None, False),
    "category": ("varchar", 64, False),
    "checksum": ("varchar", 64, False),
    "size_bytes": ("int4", None, False),
    "storage_key": ("varchar", 255, False),
    "status": ("varchar", 16, False),
    "chunk_count": ("int4", None, False),
    "error": ("text", None, True),
    "metadata": ("jsonb", None, False),
    "created_at": ("timestamptz", None, False),
    "updated_at": ("timestamptz", None, False),
}

_CHUNK_COLUMNS: dict[str, tuple[str, int | None, bool]] = {
    "id": ("uuid", None, False),
    "document_id": ("uuid", None, False),
    "chunk_index": ("int4", None, False),
    "content": ("text", None, False),
    "embedding": ("vector", None, False),
    "token_count": ("int4", None, False),
    "metadata": ("jsonb", None, False),
    "created_at": ("timestamptz", None, False),
}


def _checksum(seed: str) -> str:
    """生成 64 字符的假 checksum（与 SHA-256 同宽）。"""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _embedding() -> list[float]:
    """构造一个单位向量，避免全零向量在余弦距离下无意义。"""
    vector = [0.0] * get_settings().embedding_dim
    vector[0] = 1.0
    return vector


def _document(**overrides: Any) -> KnowledgeDocument:
    kwargs: dict[str, Any] = {
        "title": "测试文档",
        "checksum": _checksum(uuid.uuid4().hex),
    }
    kwargs.update(overrides)
    return KnowledgeDocument(**kwargs)


def _chunk(**overrides: Any) -> KnowledgeChunk:
    kwargs: dict[str, Any] = {
        "chunk_index": 0,
        "content": "测试片段",
        "embedding": _embedding(),
    }
    kwargs.update(overrides)
    return KnowledgeChunk(**kwargs)


async def _column_meta(
    session: AsyncSession, table: str
) -> dict[str, tuple[str, int | None, bool]]:
    rows = (
        await session.execute(
            text(
                "SELECT column_name, udt_name, character_maximum_length, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :table"
            ),
            {"table": table},
        )
    ).all()
    return {name: (udt, length, nullable == "YES") for name, udt, length, nullable in rows}


async def _index_defs(session: AsyncSession, table: str) -> dict[str, str]:
    rows = (
        await session.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = :table"
            ),
            {"table": table},
        )
    ).all()
    return dict(rows)


async def _create_user(session: AsyncSession) -> User:
    user = User(email=f"kb-{uuid.uuid4().hex[:8]}@example.com", password_hash="x")
    session.add(user)
    await session.flush()
    return user


async def _chunk_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(KnowledgeChunk))


async def test_document_columns_match_spec(db: None) -> None:
    async with get_session_factory()() as session:
        columns = await _column_meta(session, "knowledge_documents")
    assert columns == _DOCUMENT_COLUMNS


async def test_chunk_columns_match_spec(db: None) -> None:
    async with get_session_factory()() as session:
        columns = await _column_meta(session, "knowledge_chunks")
    assert columns == _CHUNK_COLUMNS


async def test_embedding_dimension_follows_settings(db: None) -> None:
    """vector 维度必须是 EMBEDDING_DIM，而不是写死 1536。"""
    async with get_session_factory()() as session:
        actual = await session.scalar(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = 'knowledge_chunks'::regclass AND attname = 'embedding'"
            )
        )
    assert actual == f"vector({get_settings().embedding_dim})"


async def test_document_indexes(db: None) -> None:
    async with get_session_factory()() as session:
        defs = await _index_defs(session, "knowledge_documents")

    expected = {
        "ix_knowledge_documents_status",
        "ix_knowledge_documents_category",
        "ix_knowledge_documents_owner",
        "uq_knowledge_documents_system_checksum",
        "uq_knowledge_documents_user_checksum",
    }
    # 除主键索引外不允许出现计划之外的索引。
    assert set(defs) - {"pk_knowledge_documents"} == expected

    assert "USING btree (status)" in defs["ix_knowledge_documents_status"]
    assert "USING btree (category)" in defs["ix_knowledge_documents_category"]
    # 文档列表按 owner + updated_at DESC 取最新。
    assert "USING btree (owner_user_id, updated_at DESC)" in defs["ix_knowledge_documents_owner"]

    system_def = defs["uq_knowledge_documents_system_checksum"]
    assert system_def.startswith("CREATE UNIQUE INDEX")
    assert "USING btree (checksum)" in system_def
    assert "WHERE (owner_user_id IS NULL)" in system_def

    user_def = defs["uq_knowledge_documents_user_checksum"]
    assert user_def.startswith("CREATE UNIQUE INDEX")
    assert "USING btree (owner_user_id, checksum)" in user_def
    assert "WHERE (owner_user_id IS NOT NULL)" in user_def


async def test_chunk_indexes(db: None) -> None:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT c.relname AS name, am.amname AS method, "
                    "       i.indisunique AS is_unique, c.reloptions AS reloptions, "
                    "       pg_get_indexdef(i.indexrelid) AS definition "
                    "FROM pg_index AS i "
                    "JOIN pg_class AS c ON c.oid = i.indexrelid "
                    "JOIN pg_am AS am ON am.oid = c.relam "
                    "WHERE i.indrelid = 'knowledge_chunks'::regclass"
                )
            )
        ).mappings()
    indexes = {row["name"]: dict(row) for row in rows}

    assert set(indexes) == {
        "pk_knowledge_chunks",
        "uq_knowledge_chunks_document_index",
        "ix_knowledge_chunks_embedding",
        "ix_knowledge_chunks_metadata",
    }

    unique_index = indexes["uq_knowledge_chunks_document_index"]
    assert unique_index["is_unique"] is True
    assert unique_index["method"] == "btree"
    assert "(document_id, chunk_index)" in unique_index["definition"]

    hnsw = indexes["ix_knowledge_chunks_embedding"]
    assert hnsw["method"] == "hnsw"
    assert "vector_cosine_ops" in hnsw["definition"]
    assert {"m=16", "ef_construction=64"} <= set(hnsw["reloptions"] or [])

    gin = indexes["ix_knowledge_chunks_metadata"]
    assert gin["method"] == "gin"
    assert "jsonb_path_ops" in gin["definition"]


async def test_document_server_defaults_are_applied(db: None) -> None:
    """只给 title/checksum 的裸 SQL 插入也必须能落库，其余列由库端默认值补齐。"""
    document_id = uuid.uuid4()
    checksum = _checksum("server-defaults")
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "INSERT INTO knowledge_documents (id, title, checksum) "
                "VALUES (:id, '默认值文档', :checksum)"
            ),
            {"id": document_id, "checksum": checksum},
        )
        row = (
            await session.execute(
                text(
                    "SELECT filename, source_uri, category, size_bytes, storage_key, "
                    "       status, chunk_count, error, metadata, created_at, updated_at "
                    "FROM knowledge_documents WHERE id = :id"
                ),
                {"id": document_id},
            )
        ).one()
        await session.rollback()

    mapped = row._mapping
    assert mapped["filename"] == ""
    assert mapped["source_uri"] == ""
    assert mapped["category"] == ""
    assert mapped["size_bytes"] == 0
    assert mapped["storage_key"] == ""
    assert mapped["status"] == "pending"
    assert mapped["chunk_count"] == 0
    assert mapped["error"] is None
    assert mapped["metadata"] == {}
    assert mapped["created_at"] is not None
    assert mapped["updated_at"] is not None


async def test_chunk_server_defaults_are_applied(db: None) -> None:
    document_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    vector_literal = "[" + ",".join("0" for _ in range(get_settings().embedding_dim)) + "]"
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "INSERT INTO knowledge_documents (id, title, checksum) "
                "VALUES (:id, '默认值文档', :checksum)"
            ),
            {"id": document_id, "checksum": _checksum("chunk-defaults")},
        )
        await session.execute(
            text(
                "INSERT INTO knowledge_chunks (id, document_id, chunk_index, content, embedding) "
                "VALUES (:id, :document_id, 0, '片段', CAST(:embedding AS vector))"
            ),
            {"id": chunk_id, "document_id": document_id, "embedding": vector_literal},
        )
        row = (
            await session.execute(
                text(
                    "SELECT token_count, metadata, created_at FROM knowledge_chunks WHERE id = :id"
                ),
                {"id": chunk_id},
            )
        ).one()
        await session.rollback()

    assert row._mapping["token_count"] == 0
    assert row._mapping["metadata"] == {}
    assert row._mapping["created_at"] is not None


async def test_checksum_uniqueness_is_scoped_by_owner(db: None) -> None:
    system_checksum = _checksum("系统域")
    user_checksum = _checksum("用户域")
    shared_checksum = _checksum("系统与用户可共存")

    # 1) 系统文档（owner 为 NULL）checksum 全局唯一：第二篇同 checksum 必须冲突。
    async with get_session_factory()() as session:
        session.add_all(
            [
                _document(title="系统文档 1", checksum=system_checksum),
                _document(title="系统文档 2", checksum=system_checksum),
            ]
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    # 2) 同一用户重复上传同一份文件：冲突。
    async with get_session_factory()() as session:
        user = await _create_user(session)
        session.add_all(
            [
                _document(title="A", checksum=user_checksum, owner_user_id=user.id),
                _document(title="B", checksum=user_checksum, owner_user_id=user.id),
            ]
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    # 3) 两个不同用户上传同一份文件：允许。
    async with get_session_factory()() as session:
        alice = await _create_user(session)
        bob = await _create_user(session)
        session.add_all(
            [
                _document(title="A", checksum=shared_checksum, owner_user_id=alice.id),
                _document(title="B", checksum=shared_checksum, owner_user_id=bob.id),
            ]
        )
        await session.flush()
        await session.rollback()

    # 4) 系统文档与用户文档同 checksum：允许（不同域）。
    async with get_session_factory()() as session:
        user = await _create_user(session)
        session.add_all(
            [
                _document(title="系统", checksum=shared_checksum),
                _document(title="用户", checksum=shared_checksum, owner_user_id=user.id),
            ]
        )
        await session.flush()
        await session.rollback()


async def test_status_check_constraint_rejects_unknown(db: None) -> None:
    async with get_session_factory()() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO knowledge_documents (id, title, checksum, status) "
                    "VALUES (:id, '状态非法', :checksum, 'bogus')"
                ),
                {"id": uuid.uuid4(), "checksum": _checksum("状态")},
            )
        await session.rollback()


async def test_metadata_attribute_roundtrips_jsonb(db: None) -> None:
    """``metadata_`` 属性必须映射到库里的 ``metadata`` 列。"""
    async with get_session_factory()() as session:
        document = _document(title="带元数据", metadata_={"page_count": 3, "mime": "text/markdown"})
        session.add(document)
        await session.flush()
        document_id = document.id
        await session.commit()

    async with get_session_factory()() as session:
        stored = await session.scalar(
            text("SELECT metadata FROM knowledge_documents WHERE id = :id"),
            {"id": document_id},
        )
    assert stored == {"page_count": 3, "mime": "text/markdown"}


async def test_orm_delete_cascades_to_chunks(db: None) -> None:
    async with get_session_factory()() as session:
        document = _document(title="级联删除")
        document.chunks.append(_chunk(content="片段 0"))
        session.add(document)
        await session.commit()
        document_id = document.id

    async with get_session_factory()() as session:
        document = await session.get(KnowledgeDocument, document_id)
        assert document is not None
        await session.delete(document)
        await session.commit()
        assert await _chunk_count(session) == 0


async def test_database_fk_delete_cascades_to_chunks(db: None) -> None:
    """绕过 ORM 直接 DELETE 文档，数据库外键的 ON DELETE CASCADE 也必须生效。"""
    async with get_session_factory()() as session:
        document = _document(title="数据库级联")
        document.chunks.append(_chunk(content="片段 0"))
        session.add(document)
        await session.commit()
        document_id = document.id

    async with get_session_factory()() as session:
        await session.execute(
            text("DELETE FROM knowledge_documents WHERE id = :id"), {"id": document_id}
        )
        await session.commit()
        assert await _chunk_count(session) == 0


async def test_removing_chunk_from_relationship_deletes_it(db: None) -> None:
    """delete-orphan：从集合里移除的分块会被删除，而不是留下孤儿子行。"""
    async with get_session_factory()() as session:
        document = _document(title="孤儿删除")
        document.chunks.extend([_chunk(content="片段 0"), _chunk(content="片段 1", chunk_index=1)])
        session.add(document)
        await session.commit()
        document_id = document.id

    async with get_session_factory()() as session:
        document = await session.scalar(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.id == document_id)
            .options(selectinload(KnowledgeDocument.chunks))
        )
        assert document is not None
        document.chunks.pop()
        await session.commit()
        assert await _chunk_count(session) == 1
