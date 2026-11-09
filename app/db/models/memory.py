"""长期记忆与工具调用轨迹。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Memory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "memories"
    __table_args__ = (
        CheckConstraint("kind IN ('profile', 'preference', 'fact')", name="kind"),
        CheckConstraint("status IN ('active', 'archived')", name="status"),
        Index("ix_memories_user_status_updated", "user_id", "status", text("updated_at DESC")),
        Index("ix_memories_user_kind", "user_id", "kind"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="fact", server_default="fact"
    )
    title: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", server_default=""
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    pinned: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    # 语义检索向量（M4 补 embedding 列与 pgvector 索引；当前 q 走 ILIKE）。


class ToolInvocation(Base):
    """工具调用轨迹：MCP / 原生工具 / 后续 A2A、知识检索统一记录。"""

    __tablename__ = "tool_invocations"
    __table_args__ = (
        CheckConstraint(
            "source IN ('native', 'mcp', 'a2a', 'knowledge', 'skill')", name="source"
        ),
        CheckConstraint(
            "status IN ('pending', 'ok', 'error', 'rejected', 'canceled')", name="status"
        ),
        Index("ix_tool_invocations_source_created", "source", text("created_at DESC")),
        Index("ix_tool_invocations_run_created", "run_id", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=True
    )
    tool_call_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="native", server_default="native"
    )
    source_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    args: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
