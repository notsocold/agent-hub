"""会话与消息的请求/响应模型。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

ConversationStatus = Literal["active", "archived"]
MessageRole = Literal["user", "assistant", "tool", "system"]
MessageStatus = Literal["completed", "failed", "canceled"]


class LastRunOut(BaseModel):
    id: uuid.UUID
    status: str
    model: str | None
    finished_at: datetime | None


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str
    status: ConversationStatus
    message_count: int
    last_message_preview: str
    last_message_at: datetime | None
    pending_approval_count: int
    created_at: datetime
    updated_at: datetime


class ConversationDetailOut(ConversationOut):
    archived_at: datetime | None
    last_run: LastRunOut | None


class ConversationPage(BaseModel):
    items: list[ConversationOut]
    next_cursor: str | None
    has_more: bool
    stats: dict[str, int]


class ConversationCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _normalize(self) -> ConversationCreateRequest:
        if self.title is not None:
            self.title = self.title.strip() or "新对话"
        return self


class ConversationUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    status: ConversationStatus | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> ConversationUpdateRequest:
        if self.title is None and self.status is None:
            raise ValueError("title 与 status 至少提供一个")
        if self.title is not None:
            self.title = self.title.strip()
            if not self.title:
                raise ValueError("标题不能为空")
        return self


class CitationOut(BaseModel):
    index: int
    document_id: str
    chunk_id: str
    title: str
    category: str
    score: float
    updated_at: datetime


class MessageAttachmentOut(BaseModel):
    id: str
    filename: str
    mime_type: str
    size_bytes: int


class MessageArtifactOut(BaseModel):
    id: str
    name: str
    mime_type: str
    size_bytes: int
    kind: str
    content: str | None = None
    download_url: str | None = None
    metadata: dict[str, Any] | None = None


class MessageCapabilitiesOut(BaseModel):
    skills: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    mcp_tools: list[str] = Field(default_factory=list)
    knowledge_categories: list[str] = Field(default_factory=list)
    workflow_id: str | None = None


class UsageOut(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int | None = None
    total_tokens: int | None = None


class MessageErrorOut(BaseModel):
    code: str
    message: str
    retryable: bool | None = None


class MessageOut(BaseModel):
    id: uuid.UUID
    role: MessageRole
    content: str
    content_type: str
    run_id: uuid.UUID | None
    client_message_id: str | None
    status: MessageStatus
    citations: list[CitationOut] = Field(default_factory=list)
    attachments: list[MessageAttachmentOut] = Field(default_factory=list)
    capabilities: MessageCapabilitiesOut = Field(default_factory=MessageCapabilitiesOut)
    artifacts: list[MessageArtifactOut] = Field(default_factory=list)
    usage: UsageOut | None = None
    error: MessageErrorOut | None = None
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class MessagePage(BaseModel):
    items: list[MessageOut]
    next_cursor: str | None
    has_more: bool
