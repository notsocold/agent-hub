"""审批模块 DTO。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ApprovalStatus = Literal["pending", "approved", "rejected", "expired", "canceled"]
ApprovalRisk = Literal["read", "write", "destructive", "unknown"]


class ApprovalDeciderOut(BaseModel):
    id: uuid.UUID
    display_name: str


class ApprovalOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    conversation_id: uuid.UUID
    conversation_title: str
    status: ApprovalStatus
    tool_name: str
    tool_display_name: str
    source: str
    executor: str
    target: str
    side_effect: str
    risk: ApprovalRisk
    reason: str
    payload: dict[str, Any]
    payload_preview: str
    expires_at: datetime | None
    created_at: datetime
    decided_at: datetime | None
    decided_by: ApprovalDeciderOut | None
    decision_note: str


class ApprovalPage(BaseModel):
    items: list[ApprovalOut]
    next_cursor: str | None
    has_more: bool
    stats: dict[str, int]


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    note: str = Field(default="", max_length=500)


class ApprovalDecisionResult(BaseModel):
    id: uuid.UUID
    status: ApprovalStatus
    run_id: uuid.UUID
    resumed: bool
    resume_error: str | None = None
