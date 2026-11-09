"""远程 Agent 管理 API 的请求/响应模型（字段与前端 agent.ts 对齐）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

AgentHealthStatus = Literal["unknown", "healthy", "degraded", "unhealthy"]
AgentAuthType = Literal["none", "api_key", "oauth2"]

_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"


class AgentAuthInput(BaseModel):
    """注册/更新时的鉴权信息；secret_ref 只存引用（如 ``env:DATA_AGENT_KEY``）。"""

    type: AgentAuthType = "none"
    secret_ref: str | None = Field(default=None, max_length=200)

    @field_validator("secret_ref")
    @classmethod
    def _secret_ref(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if value and not value.startswith("env:"):
            raise ValueError("secret_ref 必须为 env:环境变量名 形式")
        return value


class AgentPreviewRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    slug: str | None = Field(default=None, pattern=_SLUG_PATTERN)
    auth: AgentAuthInput | None = None


class AgentSkillRef(BaseModel):
    id: str
    name: str
    description: str | None = None


class AgentCapabilitiesOut(BaseModel):
    streaming: bool = False
    push_notifications: bool = False


class AgentNormalizedCard(BaseModel):
    name: str
    description: str
    url: str
    protocol_version: str
    skills: list[AgentSkillRef]
    capabilities: AgentCapabilitiesOut


class AgentPreviewResult(BaseModel):
    valid: bool
    card: dict[str, Any]
    normalized: AgentNormalizedCard
    warnings: list[str] = Field(default_factory=list)


class AgentCreateRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    slug: str | None = Field(default=None, pattern=_SLUG_PATTERN)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    auth: AgentAuthInput | None = None


class AgentUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None
    auth: AgentAuthInput | None = None
    headers: dict[str, str] | None = None


class AgentRecentTask(BaseModel):
    id: uuid.UUID
    a2a_task_id: str
    state: str
    duration_ms: int | None
    finished_at: datetime | None


class AgentOut(BaseModel):
    """列表项：前端 Agent 类型全字段。"""

    id: uuid.UUID
    slug: str
    name: str
    description: str
    url: str
    protocol_version: str
    skills: list[AgentSkillRef]
    skill_count: int
    auth_type: AgentAuthType
    auth_scheme: str
    auth_hint: str
    capabilities: AgentCapabilitiesOut
    health_status: AgentHealthStatus
    enabled: bool
    last_checked_at: datetime | None
    last_latency_ms: int | None
    last_error: str | None
    recent_task: AgentRecentTask | None
    created_at: datetime
    updated_at: datetime


class AgentDetailOut(AgentOut):
    card: dict[str, Any] | None = None
    recent_tasks: list[AgentRecentTask] = Field(default_factory=list)
    requires_approval: bool = False


class AgentStats(BaseModel):
    total: int = 0
    healthy: int = 0
    degraded: int = 0
    unhealthy: int = 0
    disabled: int = 0
    enabled: int = 0


class AgentPage(BaseModel):
    items: list[AgentOut]
    next_cursor: str | None
    has_more: bool
    stats: AgentStats


class AgentRefreshResult(BaseModel):
    agent: AgentDetailOut
    changed: bool
    changed_fields: list[str] = Field(default_factory=list)


class AgentHealthResult(BaseModel):
    health_status: AgentHealthStatus
    latency_ms: int | None
    checked_at: datetime
    error: str | None
