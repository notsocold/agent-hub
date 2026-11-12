"""M9 工作流 CRUD DTO。

``name`` 在 strip 后校验 1–100 字符，且不得含双引号与换行（含 ``\\r``）——
T2 的 ``prompt_section`` 用 ``<workflow name="…">`` 锚定渲染，这两类字符会破坏
段落结构，因此在 DTO 层直接拒绝（违规 422）。``steps`` 的元素类型刻意放宽为
``Any``：形状约束唯一口径是装配器 ``validate_steps``（错误码统一
``workflow_steps_invalid``），schema 层只保证「是数组」，不自造结构校验。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, field_validator

WorkflowScope = Literal["system", "user"]

# name 的禁用字符：双引号破坏 <workflow name="…"> 锚点；\n/\r 属换行。
_NAME_FORBIDDEN_CHARS = ('"', "\n", "\r")


def _validated_name(value: str) -> str:
    """name 公共校验：strip 后 1–100 字符，不含双引号与换行。"""
    stripped = value.strip()
    if not 1 <= len(stripped) <= 100:
        raise ValueError("名称需为 1–100 个字符")
    if any(char in stripped for char in _NAME_FORBIDDEN_CHARS):
        raise ValueError("名称不能包含双引号或换行符")
    return stripped


def _validated_description(value: str) -> str:
    """description 公共校验：strip 后 ≤500 字符（允许空串）。"""
    stripped = value.strip()
    if len(stripped) > 500:
        raise ValueError("描述不能超过 500 字符")
    return stripped


class WorkflowCreate(BaseModel):
    """创建请求：``name``/``steps`` 必填，``description`` 缺省为空串。"""

    name: str
    description: str = ""
    steps: list[Any]

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _validated_name(value)

    @field_validator("description")
    @classmethod
    def _check_description(cls, value: str) -> str:
        return _validated_description(value)


class WorkflowUpdate(BaseModel):
    """PATCH 请求：全部可选，``None`` 表示不修改该字段。"""

    name: str | None = None
    description: str | None = None
    steps: list[Any] | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str | None) -> str | None:
        return None if value is None else _validated_name(value)

    @field_validator("description")
    @classmethod
    def _check_description(cls, value: str | None) -> str | None:
        return None if value is None else _validated_description(value)


class WorkflowEnabledRequest(BaseModel):
    """``PUT /workflows/{id}/enabled`` 请求体。"""

    enabled: bool


class WorkflowOut(BaseModel):
    """工作流视图：列表与详情共用。"""

    id: uuid.UUID
    scope: WorkflowScope
    name: str
    description: str
    steps: list[dict[str, Any]]
    enabled: bool
    version: int
    created_at: datetime
    updated_at: datetime


class WorkflowListOut(BaseModel):
    """游标分页列表：``next_cursor`` 为空表示没有更多页。"""

    items: list[WorkflowOut]
    next_cursor: str | None
    has_more: bool


class WorkflowRunOut(BaseModel):
    """工作流运行记录视图：``run_*`` 字段来自 join 的 ``runs`` 行，无对应行时为 None。

    ``status`` 是映射行自身状态（创建即 ``running``，由 chat 终态同步追平）；
    ``run_*`` 是 chat run 的终态快照（status/finished_at/duration_ms/error_code）。
    """

    id: uuid.UUID
    run_id: uuid.UUID
    status: str
    created_at: datetime
    run_status: str | None
    run_finished_at: datetime | None
    run_duration_ms: int | None
    run_error_code: str | None


class WorkflowRunListOut(BaseModel):
    """运行历史游标分页列表：按映射行 ``created_at DESC`` 排序。"""

    items: list[WorkflowRunOut]
    next_cursor: str | None
    has_more: bool
