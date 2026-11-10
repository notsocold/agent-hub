"""审计日志检索的响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AuditLogOut(BaseModel):
    id: int
    actor_type: str
    actor_id: str
    action: str
    resource_type: str
    resource_id: str
    detail: dict[str, Any]
    request_id: str
    created_at: datetime


class AuditLogPage(BaseModel):
    items: list[AuditLogOut]
    next_cursor: str | None = Field(
        default=None, description="下一页游标；`has_more=false` 时为 null"
    )
    has_more: bool
