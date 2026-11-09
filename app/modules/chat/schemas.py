"""聊天、运行与运行事件的请求/响应模型。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

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
    knowledge_categories: list[str] = Field(default_factory=list, max_length=5)
    workflow_id: uuid.UUID | None = None
    workflow_version: int | None = Field(default=None, ge=1)

    @field_validator("message")
    @classmethod
    def _message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("消息不能为空")
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
    id: str
    seq: int
    ts: datetime
    type: str
    status: str
    title: str
    subtitle: str
    duration_ms: int | None
    tokens: dict[str, Any] | None = None
    payload: dict[str, Any]
    error: str | None = None


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
