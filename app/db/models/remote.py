"""远程 Agent（A2A 出站）与 A2A 任务。

- ``remote_agents``：v1 全局注册表；``owner_user_id`` 保留可空字段，
  默认不过滤归属。slug 的部分唯一索引按 ``deleted_at IS NULL`` 限定，
  软删后可重新注册同名 Agent。
- ``a2a_tasks``：每次出站委派一行；``a2a_task_id`` 为远程任务 ID，
  ``timeline`` 记录状态转移（每条带时长），请求/结果/错误均落 JSONB。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin

A2A_TASK_STATES = (
    "submitted",
    "working",
    "input_required",
    "auth_required",
    "completed",
    "failed",
    "canceled",
    "rejected",
)

A2A_ACTIVE_STATES = ("submitted", "working", "input_required", "auth_required")


class RemoteAgent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "remote_agents"
    __table_args__ = (
        CheckConstraint("auth_type IN ('none', 'api_key', 'oauth2')", name="auth_type"),
        CheckConstraint(
            "health_status IN ('unknown', 'healthy', 'degraded', 'unhealthy')",
            name="health_status",
        ),
        Index(
            "uq_remote_agents_slug_active",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_remote_agents_owner", "owner_user_id"),
        Index("ix_remote_agents_enabled_health", "enabled", "health_status"),
    )

    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    protocol_version: Mapped[str] = mapped_column(
        String(16), nullable=False, default="", server_default=""
    )
    # Agent Card 原样快照（原始字段名，方便前端抽屉展示）。
    card: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    skills: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    capabilities: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    auth_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="none", server_default="none"
    )
    # 展示用：bearer / header / oauth2 / none
    auth_scheme: Mapped[str] = mapped_column(
        String(32), nullable=False, default="none", server_default="none"
    )
    auth_hint: Mapped[str] = mapped_column(
        String(160), nullable=False, default="", server_default=""
    )
    # 凭据引用（env:NAME）；不回显明文。
    secret_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 请求头：普通头明文、敏感头只存 secret_ref 或 AES-GCM 密文。
    headers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # Card 技能声明写操作需审批时置真；调用前走 HITL 审批链路。
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    health_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", server_default="unknown"
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # name 被用户手动修改过后，refresh 不再覆盖。
    name_edited: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class A2aTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "a2a_tasks"
    __table_args__ = (
        CheckConstraint(
            "state IN ('submitted', 'working', 'input_required', 'auth_required', "
            "'completed', 'failed', 'canceled', 'rejected')",
            name="state",
        ),
        Index("ix_a2a_tasks_state_created", "state", text("created_at DESC")),
        Index("ix_a2a_tasks_agent_created", "remote_agent_id", text("created_at DESC")),
        Index("ix_a2a_tasks_run_created", "run_id", text("created_at DESC")),
        Index("ix_a2a_tasks_conversation_created", "conversation_id", text("created_at DESC")),
    )

    a2a_task_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    remote_agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("remote_agents.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    tool_call_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    # 远程上下文 ID；默认 = conversation_id，远程返回后优先用远程值。
    context_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="submitted", server_default="submitted"
    )
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    # {tool, args, summary}：工具调用快照。
    request: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # {artifact: {type, summary, parts, data}, summary}：终态结果。
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # {code, message, retryable, at}：失败错误。
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # {question, input_schema, expires_at}：远程要求补充输入。
    input_request: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # {scopes, authorization_url, expires_at}：远程要求授权。
    auth_request: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # [{state, at, duration_ms, note}]：状态转移轨迹。
    timeline: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # updated_at 复用 TimestampMixin（onupdate=func.now()）。


__all__ = ["A2A_ACTIVE_STATES", "A2A_TASK_STATES", "A2aTask", "RemoteAgent"]
