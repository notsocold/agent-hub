"""长期记忆模块 DTO。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

MemoryKind = Literal["profile", "preference", "fact"]
MemoryStatus = Literal["active", "archived"]


class MemoryOut(BaseModel):
    id: uuid.UUID
    kind: MemoryKind
    title: str
    content: str
    pinned: bool
    status: MemoryStatus
    source: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class MemoryPage(BaseModel):
    items: list[MemoryOut]
    next_cursor: str | None
    has_more: bool
    stats: dict[str, int]


class MemoryStatsOut(BaseModel):
    total: int
    profile: int
    preference: int
    fact: int
    active: int
    archived: int


class MemoryCreateRequest(BaseModel):
    kind: MemoryKind = "fact"
    title: str = Field(default="", max_length=200)
    content: str = Field(min_length=1, max_length=1000)
    pinned: bool = False

    @model_validator(mode="after")
    def _normalize(self) -> MemoryCreateRequest:
        self.title = self.title.strip()
        self.content = self.content.strip()
        if not self.content:
            raise ValueError("内容不能为空")
        return self


class MemoryUpdateRequest(BaseModel):
    kind: MemoryKind | None = None
    title: str | None = Field(default=None, max_length=200)
    content: str | None = Field(default=None, min_length=1, max_length=1000)
    pinned: bool | None = None
    status: MemoryStatus | None = None


class MemoryExportOut(BaseModel):
    version: int = 1
    exported_at: datetime
    items: list[MemoryOut]
