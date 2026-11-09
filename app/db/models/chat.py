"""运行（run）与运行事件（轨迹）。"""

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
from app.db.mixins import UUIDPrimaryKeyMixin

_ACTIVE_RUN_STATUSES = "status IN ('queued', 'running', 'interrupted')"


class Run(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'canceled', 'interrupted')",
            name="status",
        ),
        CheckConstraint("source IN ('user', 'a2a', 'system')", name="source"),
        Index("ix_runs_conversation_created", "conversation_id", text("created_at DESC")),
        Index("ix_runs_status", "status"),
        # 同一会话同时只允许一个未结束的 run；并发发消息由数据库兜底。
        Index(
            "uq_runs_active_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text(_ACTIVE_RUN_STATUSES),
        ),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "messages.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_runs_user_message_id_messages",
        ),
        nullable=True,
    )
    assistant_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "messages.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_runs_assistant_message_id_messages",
        ),
        nullable=True,
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user", server_default="user"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running", server_default="running"
    )
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    reasoning_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    reasoning_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    first_token_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    capabilities: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    workflow_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        Index("uq_run_events_run_seq", "run_id", "seq", unique=True),
        Index("ix_run_events_run_ts", "run_id", "ts"),
        Index("ix_run_events_type_ts", "type", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="", server_default="")
    subtitle: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", server_default=""
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
