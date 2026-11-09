"""MCP 服务器、工具与用户级启停覆盖。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
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


class McpServer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "mcp_servers"
    __table_args__ = (
        CheckConstraint("scope IN ('system', 'user')", name="scope"),
        CheckConstraint(
            "transport IN ('streamable_http', 'sse', 'stdio')", name="transport"
        ),
        CheckConstraint("scope = 'user' OR owner_user_id IS NULL", name="owner_rule"),
        CheckConstraint(
            "(transport = 'stdio' AND command IS NOT NULL) OR "
            "(transport <> 'stdio' AND url IS NOT NULL)",
            name="transport_required",
        ),
        Index(
            "uq_mcp_servers_system_slug",
            "slug",
            unique=True,
            postgresql_where=text("scope = 'system'"),
        ),
        Index(
            "uq_mcp_servers_user_slug",
            "owner_user_id",
            "slug",
            unique=True,
            postgresql_where=text("scope = 'user'"),
        ),
        Index("ix_mcp_servers_owner_enabled", "owner_user_id", "enabled"),
        Index("ix_mcp_servers_scope_enabled", "scope", "enabled"),
    )

    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    transport: Mapped[str] = mapped_column(String(24), nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    command: Mapped[str | None] = mapped_column(String(512), nullable=True)
    args: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    env_refs: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # 请求头：普通头明文、敏感头只存 secret_ref 或 AES-GCM 密文，绝不回显。
    headers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    credentials_hint: Mapped[str] = mapped_column(
        String(160), nullable=False, default="", server_default=""
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    auto_inject: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    force_disabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    health_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", server_default="unknown"
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tools_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class McpTool(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "mcp_tools"
    __table_args__ = (
        CheckConstraint("risk IN ('read', 'write', 'destructive', 'unknown')", name="risk"),
        Index("uq_mcp_tools_server_name", "server_id", "name", unique=True),
        Index("ix_mcp_tools_server_enabled", "server_id", "enabled"),
    )

    server_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    exposed_name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    input_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    annotations: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    risk: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", server_default="unknown"
    )
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    admin_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    user_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class UserCapabilityOverride(Base):
    """系统级能力项（系统技能/系统 MCP）的按用户启停覆盖。"""

    __tablename__ = "user_capability_overrides"
    __table_args__ = (
        CheckConstraint("capability IN ('mcp_server', 'skill')", name="capability"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    capability: Mapped[str] = mapped_column(String(16), primary_key=True)
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
