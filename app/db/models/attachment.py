"""聊天附件（本地磁盘存储 + 数据库索引）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import UUIDPrimaryKeyMixin


class Attachment(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "attachments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('processing', 'ready', 'failed', 'deleted')", name="status"
        ),
        Index("ix_attachments_user_created", "user_id", "created_at"),
        Index("ix_attachments_user_checksum", "user_id", "checksum"),
        Index("ix_attachments_conversation_created", "conversation_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    size_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    storage_path: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", server_default=""
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ready", server_default="ready"
    )
    purpose: Mapped[str] = mapped_column(
        String(32), nullable=False, default="chat", server_default="chat"
    )
    text_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
