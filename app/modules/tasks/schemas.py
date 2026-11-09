"""A2A 任务查询与动作 API 的请求/响应模型（字段与前端 task.ts 对齐）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

A2aTaskState = Literal[
    "submitted",
    "working",
    "input_required",
    "auth_required",
    "completed",
    "failed",
    "canceled",
    "rejected",
]
TaskAction = Literal["cancel", "resume", "authorize", "reject"]


class TaskAgentRef(BaseModel):
    id: uuid.UUID
    slug: str
    name: str


class TaskConversationRef(BaseModel):
    id: uuid.UUID
    title: str


class TaskTimelineEntry(BaseModel):
    state: A2aTaskState
    at: datetime
    duration_ms: int | None
    note: str = ""


class TaskInputRequest(BaseModel):
    question: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None


class TaskAuthRequest(BaseModel):
    scopes: list[str] = Field(default_factory=list)
    authorization_url: str | None
    expires_at: datetime | None


class TaskResultArtifact(BaseModel):
    type: str
    summary: str
    parts: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class TaskErrorOut(BaseModel):
    code: str
    message: str
    retryable: bool
    at: datetime | None


class A2aTaskListItem(BaseModel):
    id: uuid.UUID
    a2a_task_id: str
    agent: TaskAgentRef
    run_id: uuid.UUID | None
    conversation: TaskConversationRef | None
    state: A2aTaskState
    progress: float | None
    duration_ms: int | None
    request_summary: str
    error_summary: str | None
    available_actions: list[TaskAction]
    created_at: datetime
    updated_at: datetime


class A2aTaskDetail(A2aTaskListItem):
    tool_call_id: str
    request: dict[str, Any] | None
    result: dict[str, Any] | None
    error: TaskErrorOut | None
    input_request: TaskInputRequest | None
    auth_request: TaskAuthRequest | None
    timeline: list[TaskTimelineEntry]
    started_at: datetime | None
    finished_at: datetime | None


class TaskStats(BaseModel):
    total: int = 0
    submitted: int = 0
    working: int = 0
    input_required: int = 0
    auth_required: int = 0
    completed: int = 0
    failed: int = 0
    canceled: int = 0
    rejected: int = 0


class TaskPage(BaseModel):
    items: list[A2aTaskListItem]
    next_cursor: str | None
    has_more: bool
    stats: TaskStats


class TaskResumeRequest(BaseModel):
    input: str = Field(min_length=1, max_length=4000)


class TaskAuthorizeRequest(BaseModel):
    scopes: list[str] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=1000)


class TaskRejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class TaskRunRef(BaseModel):
    id: uuid.UUID
    status: str


class TaskResumeResult(BaseModel):
    task: A2aTaskDetail
    run: TaskRunRef | None = None
