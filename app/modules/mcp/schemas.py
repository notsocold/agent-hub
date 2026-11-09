"""MCP 模块 DTO。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

McpTransport = Literal["streamable_http", "sse", "stdio"]
McpHealth = Literal["unknown", "healthy", "unhealthy", "disabled"]
McpRisk = Literal["read", "write", "destructive", "unknown"]


class McpServerCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    description: str = Field(default="", max_length=2000)
    transport: McpTransport = "streamable_http"
    url: str | None = Field(default=None, max_length=2048)
    headers: dict[str, str] = Field(default_factory=dict)
    command: str | None = Field(
        default=None, max_length=512, description="stdio 传输必填（仅管理员可用）"
    )
    args: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("name", "description")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class McpServerUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    url: str | None = Field(default=None, max_length=2048)
    headers: dict[str, str] | None = None
    auto_inject: bool | None = None


class McpServerEnabledRequest(BaseModel):
    enabled: bool


class McpToolOut(BaseModel):
    id: uuid.UUID
    server_id: uuid.UUID
    name: str
    exposed_name: str
    description: str
    input_schema: dict[str, Any]
    risk: McpRisk
    requires_approval: bool
    admin_only: bool
    user_allowed: bool = Field(description="当前用户是否可调用（系统 MCP 写工具对普通用户为 false）")
    enabled: bool
    discovered_at: datetime
    updated_at: datetime


class McpServerOut(BaseModel):
    id: uuid.UUID
    scope: str
    slug: str
    name: str
    description: str
    transport: str
    url: str | None
    enabled: bool
    auto_inject: bool
    force_disabled: bool
    effective_enabled: bool
    health_status: McpHealth
    last_checked_at: datetime | None
    last_error: str | None
    latency_ms: int | None
    tool_count: int
    credentials_hint: str
    created_at: datetime
    updated_at: datetime


class McpServerListOut(BaseModel):
    items: list[McpServerOut]
    stats: dict[str, int] = Field(description="total/system/user/enabled/healthy")


class McpCheckResult(BaseModel):
    id: uuid.UUID
    health_status: McpHealth
    latency_ms: int | None
    message: str | None
    tools_discovered: int = 0
    last_checked_at: datetime | None


class McpToolPatchRequest(BaseModel):
    enabled: bool | None = None
    requires_approval: bool | None = None


class McpAvailableServerOut(BaseModel):
    id: uuid.UUID
    scope: str
    slug: str
    name: str
    transport: str
    effective_enabled: bool
    health_status: McpHealth
    tool_count: int


class McpAvailableListOut(BaseModel):
    items: list[McpAvailableServerOut]
