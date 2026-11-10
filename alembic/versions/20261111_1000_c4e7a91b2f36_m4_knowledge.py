"""m4 knowledge tables

Revision ID: c4e7a91b2f36
Revises: 8a2f7c31e9d5
Create Date: 2026-11-11 10:00:00

M4 知识库：文档表与分块表（pgvector 向量列 + HNSW / GIN 索引）。

- ``embedding`` 维度取自 ``EMBEDDING_DIM``。pgvector 的 ``vector(n)`` 无法原地改维度，
  调整该配置后必须重建 ``knowledge_chunks`` 表（不能靠 ALTER）。
- ``CREATE EXTENSION IF NOT EXISTS vector`` 为幂等创建；``downgrade()`` 只删表，不回退扩展。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import get_settings

revision: str = "c4e7a91b2f36"
down_revision: str | None = "8a2f7c31e9d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 向量扩展由 superuser 预装；幂等创建，降级时不回退（其它表/功能可能也依赖它）。
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("filename", sa.String(length=255), server_default="", nullable=False),
        sa.Column("source_uri", sa.Text(), server_default="", nullable=False),
        sa.Column("category", sa.String(length=64), server_default="", nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("storage_key", sa.String(length=255), server_default="", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'indexing', 'ready', 'failed')",
            name=op.f("ck_knowledge_documents_status"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_knowledge_documents_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_documents")),
    )
    op.create_index(
        "ix_knowledge_documents_status",
        "knowledge_documents",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_documents_category",
        "knowledge_documents",
        ["category"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_documents_owner",
        "knowledge_documents",
        ["owner_user_id", sa.text("updated_at DESC")],
        unique=False,
    )
    # checksum 唯一性按归属分域：系统文档全局唯一，用户文档按用户唯一。
    op.create_index(
        "uq_knowledge_documents_system_checksum",
        "knowledge_documents",
        ["checksum"],
        unique=True,
        postgresql_where=sa.text("owner_user_id IS NULL"),
    )
    op.create_index(
        "uq_knowledge_documents_user_checksum",
        "knowledge_documents",
        ["owner_user_id", "checksum"],
        unique=True,
        postgresql_where=sa.text("owner_user_id IS NOT NULL"),
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        # 维度随 EMBEDDING_DIM；改维度必须重建本表。
        sa.Column("embedding", Vector(get_settings().embedding_dim), nullable=False),
        sa.Column("token_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_documents.id"],
            name=op.f("fk_knowledge_chunks_document_id_knowledge_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_chunks")),
        sa.UniqueConstraint(
            "document_id",
            "chunk_index",
            name=op.f("uq_knowledge_chunks_document_index"),
        ),
    )
    op.create_index(
        "ix_knowledge_chunks_embedding",
        "knowledge_chunks",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "ix_knowledge_chunks_metadata",
        "knowledge_chunks",
        ["metadata"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"metadata": "jsonb_path_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_chunks_metadata", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_embedding", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_index("uq_knowledge_documents_user_checksum", table_name="knowledge_documents")
    op.drop_index("uq_knowledge_documents_system_checksum", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_owner", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_category", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_status", table_name="knowledge_documents")
    op.drop_table("knowledge_documents")
