"""管理台请求/响应模型。

用户是管理台唯一的全新业务对象：响应永不包含 ``password_hash``；PATCH 只允许
``role``/``status`` 两项（``extra="forbid"``），且必须至少提交一项非空值。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

AdminUserRole = Literal["user", "admin"]
AdminUserStatus = Literal["active", "disabled"]


class AdminUserOut(BaseModel):
    """管理台用户输出；与列表项同形，不含密码散列。"""

    id: uuid.UUID
    email: str
    display_name: str
    role: AdminUserRole
    status: AdminUserStatus
    last_login_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AdminUserStats(BaseModel):
    """当前筛选条件下的全量计数。"""

    total: int
    active: int
    disabled: int
    admins: int


class AdminUserPage(BaseModel):
    items: list[AdminUserOut]
    next_cursor: str | None
    has_more: bool
    stats: AdminUserStats


class AdminUserPatchRequest(BaseModel):
    """管理员修改用户角色/状态：只改这两项，且至少一项非空。"""

    model_config = ConfigDict(extra="forbid")

    role: AdminUserRole | None = None
    status: AdminUserStatus | None = None

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> AdminUserPatchRequest:
        if self.role is None and self.status is None:
            raise ValueError("至少需要修改 role 或 status 中的一项")
        return self
