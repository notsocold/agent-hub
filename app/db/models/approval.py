"""人工审批（HITL）最小切片。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import UUIDPrimaryKeyMixin


class Approval(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired', 'canceled')",
            name="status",
        ),
        CheckConstraint("risk IN ('read', 'write', 'destructive', 'unknown')", name="risk"),
        Index("ix_approvals_status_created", "status", "created_at"),
        Index("ix_approvals_run_id", "run_id"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    a2a_task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_display_name: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", server_default=""
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="native", server_default="native"
    )
    executor: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", server_default=""
    )
    target: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    side_effect: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    risk: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", server_default="unknown"
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    payload_preview: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", server_default=""
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decision_note: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
