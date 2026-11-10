"""聊天、运行与运行事件的请求/响应模型。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.core.config import get_settings
from app.modules.conversations.schemas import MessageCapabilitiesOut, MessageErrorOut, UsageOut

RunStatus = Literal["queued", "running", "succeeded", "failed", "canceled", "interrupted"]
RunPendingKind = Literal["approval", "input_required", "auth_required"]


class ChatStreamRequest(BaseModel):
    conversation_id: uuid.UUID | None = None
    message: str = Field(min_length=1, max_length=8000)
    client_message_id: str | None = Field(default=None, max_length=64)
    attachments: list[uuid.UUID] = Field(default_factory=list, max_length=5)
    skills: list[uuid.UUID] = Field(default_factory=list, max_length=3)
    mcp_servers: list[uuid.UUID] = Field(default_factory=list, max_length=5)
    mcp_tools: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    model: str | None = Field(default=None, max_length=64)
    reasoning_effort: str | None = Field(default=None, max_length=16)
    knowledge_categories: list[str] = Field(default_factory=list)
    workflow_id: uuid.UUID | None = None
    workflow_version: int | None = Field(default=None, ge=1)

    @field_validator("message")
    @classmethod
    def _message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("消息不能为空")
        return value

    @field_validator("knowledge_categories")
    @classmethod
    def _knowledge_categories_limit(cls, value: list[str]) -> list[str]:
        """上限跟随 ``KNOWLEDGE_CATEGORIES_PER_MESSAGE``（默认 5）。

        ``Field(max_length=...)`` 在类定义时求值，读不到运行时 Settings（测试还会改
        环境变量），因此用字段校验器在读请求时读取配置；超限返回 422。
        """
        limit = get_settings().knowledge_categories_per_message
        if len(value) > limit:
            raise ValueError(f"知识库分类最多 {limit} 个")
        return value


class ChatStartResult(BaseModel):
    run_id: uuid.UUID
    conversation_id: uuid.UUID
    replay: bool = False


class RunPendingOut(BaseModel):
    kind: RunPendingKind | None = None
    approval_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None


class RunMessageIdsOut(BaseModel):
    user: uuid.UUID | None = None
    assistant: uuid.UUID | None = None


class RunWorkflowOut(BaseModel):
    id: uuid.UUID
    name: str
    version: int


class RunAgentOut(BaseModel):
    kind: Literal["local", "a2a"]
    id: str
    name: str
    model: str | None = None
    slug: str | None = None
    skill_id: str | None = None
    state: str | None = None


class RunOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    status: RunStatus
    source: str
    model: str | None
    reasoning_effort: str | None
    workflow: RunWorkflowOut | None
    capabilities: MessageCapabilitiesOut
    usage: UsageOut
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    active_duration_ms: int | None
    first_token_ms: int | None
    pending: RunPendingOut
    message_ids: RunMessageIdsOut
    agents: list[RunAgentOut]
    error: MessageErrorOut | None


class RunEventOut(BaseModel):
    id: str = Field(description="不透明事件 ID，格式为 `<run_id>:<seq>`，前端不得解析")
    seq: int = Field(description="运行内单调递增序号；事件回放时允许跳号")
    ts: datetime
    type: str
    status: str
    title: str
    subtitle: str
    duration_ms: int | None
    tokens: dict[str, Any] | None = Field(
        description="该事件的 token 用量；没有 token 的事件返回 null。该键始终存在"
    )
    payload: dict[str, Any] = Field(description="事件负载；内部字段 `_event` 已剔除")
    error: str | None = Field(
        description="错误摘要字符串；无错误返回 null。结构化错误在 message.done/error 事件里"
    )


class RunEventPage(BaseModel):
    items: list[RunEventOut]
    next_cursor: str | None
    has_more: bool


class RunCancelResult(BaseModel):
    id: uuid.UUID
    status: RunStatus
    canceled_at: datetime


class RunResumeRequest(BaseModel):
    input: str = Field(min_length=1, max_length=4000)


class RunResumeResult(BaseModel):
    run_id: uuid.UUID
    status: RunStatus
    resumed: bool
