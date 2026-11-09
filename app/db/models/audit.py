"""审计日志（M1.5 起写入，本阶段先建表）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Identity, Index, String, func, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_created", text("created_at DESC")),
        Index("ix_audit_logs_actor_created", "actor_id", text("created_at DESC")),
        Index("ix_audit_logs_action_created", "action", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    resource_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    request_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
