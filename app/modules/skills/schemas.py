"""技能模块 DTO。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

SkillScope = Literal["system", "user"]


class SkillFileOut(BaseModel):
    path: str
    size_bytes: int


class SkillOut(BaseModel):
    id: uuid.UUID
    scope: SkillScope
    slug: str
    name: str
    description: str
    version: str
    source: str
    enabled: bool
    force_disabled: bool
    effective_enabled: bool = Field(description="force_disabled/用户覆盖合并后的有效启停")
    estimated_tokens: int
    size_bytes: int
    file_count: int
    created_at: datetime
    updated_at: datetime


class SkillDetailOut(SkillOut):
    content: str = Field(description="SKILL.md 正文；列表接口不含该字段")
    metadata: dict[str, Any] = Field(default_factory=dict)
    files: list[SkillFileOut] = Field(default_factory=list)


class SkillListOut(BaseModel):
    items: list[SkillOut]
    stats: dict[str, int] = Field(description="total/system/user/enabled")


class SkillCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=200_000)

    @field_validator("name", "description", "content")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class SkillUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, min_length=1, max_length=500)
    content: str | None = Field(default=None, min_length=1, max_length=200_000)


class SkillEnabledRequest(BaseModel):
    enabled: bool


class SkillImportResult(SkillDetailOut):
    imported: bool = True
    ignored: list[str] = Field(default_factory=list, description="被忽略的 scripts/可执行/二进制文件")


class SkillCatalogOut(BaseModel):
    id: uuid.UUID
    scope: SkillScope
    slug: str
    name: str
    description: str
    effective_enabled: bool
    estimated_tokens: int


class SkillCatalogListOut(BaseModel):
    items: list[SkillCatalogOut]


class SkillFileContentOut(BaseModel):
    skill_id: uuid.UUID
    path: str
    content: str
    size_bytes: int
