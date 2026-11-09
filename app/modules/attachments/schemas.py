"""附件模块 DTO。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

AttachmentStatus = Literal["processing", "ready", "failed", "deleted"]
AttachmentPurpose = Literal["chat", "general"]


class AttachmentOut(BaseModel):
    id: uuid.UUID
    filename: str
    mime_type: str
    size_bytes: int
    checksum: str
    status: AttachmentStatus
    purpose: str
    conversation_id: uuid.UUID | None
    created_at: datetime
    expires_at: datetime | None
    duplicate: bool = Field(
        default=False, description="本次上传命中了同用户同 checksum 的已有附件"
    )


class AttachmentPage(BaseModel):
    items: list[AttachmentOut]
    next_cursor: str | None
    has_more: bool
