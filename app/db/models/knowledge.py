"""知识库文档与分块（pgvector 向量检索）。

- ``knowledge_documents``：系统文档（``owner_user_id IS NULL``）与用户私有文档共用一张表；
  checksum 唯一性按归属分域，避免「两个用户上传同一份公开文档」互相冲突。
- ``knowledge_chunks``：文档切分后的正文与向量，文档删除时分块随外键级联删除。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import get_settings
from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin

# 向量维度取自 EMBEDDING_DIM。注意：pgvector 的 vector(n) 无法原地改维度，
# 修改该配置后必须重建 knowledge_chunks 表（迁移 docstring 亦有说明）。
_EMBEDDING_DIM = get_settings().embedding_dim


class KnowledgeDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """知识库文档。"""

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'indexing', 'ready', 'failed')", name="status"),
        # checksum 唯一性分域：系统文档全局唯一，用户文档按用户唯一。
        Index(
            "uq_knowledge_documents_system_checksum",
            "checksum",
            unique=True,
            postgresql_where=text("owner_user_id IS NULL"),
        ),
        Index(
            "uq_knowledge_documents_user_checksum",
            "owner_user_id",
            "checksum",
            unique=True,
            postgresql_where=text("owner_user_id IS NOT NULL"),
        ),
        Index("ix_knowledge_documents_status", "status"),
        Index("ix_knowledge_documents_category", "category"),
        Index("ix_knowledge_documents_owner", "owner_user_id", text("updated_at DESC")),
    )

    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    filename: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    source_uri: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    storage_key: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 页数、mime、字符数等；属性名加下划线避开 Declarative 基类的 metadata，列名仍是 metadata。
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    chunks: Mapped[list[KnowledgeChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class KnowledgeChunk(UUIDPrimaryKeyMixin, Base):
    """知识库分块；只保留 created_at 与文档 updated_at 对齐（不维护 updated_at）。"""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunks_document_index"),
        Index(
            "ix_knowledge_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index(
            "ix_knowledge_chunks_metadata",
            "metadata",
            postgresql_using="gin",
            postgresql_ops={"metadata": "jsonb_path_ops"},
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 维度来自配置；改维度必须重建表，不能原地 ALTER。
    embedding: Mapped[list[float]] = mapped_column(Vector(_EMBEDDING_DIM), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # 模型属性名 metadata_ 避免与 Declarative 基类的 metadata 冲突，落库列名仍是 metadata。
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    document: Mapped[KnowledgeDocument] = relationship(back_populates="chunks")
