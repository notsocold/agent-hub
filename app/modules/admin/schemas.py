"""管理台请求/响应模型。

用户是管理台唯一的全新业务对象：响应永不包含 ``password_hash``；PATCH 只允许
``role``/``status`` 两项（``extra="forbid"``），且必须至少提交一项非空值。
概览是只读计数的固定形状 DTO：所有状态键始终存在，空库时为 ``0``。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.skills.schemas import SkillOut

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


class AdminInstructionOut(BaseModel):
    """平台默认指令视图；GET/PUT 同形。

    ``content`` 就是无个人覆盖用户实际注入的文本，与 ``effective_preview`` 同义，
    故不再额外返回（R5）。``user_override_count`` 统计
    ``user_settings.instructions IS NOT NULL`` 的用户数；``updated_by`` 为最后
    保存该配置的管理员，用户已删除时为 null。
    """

    content: str
    is_default: bool
    max_chars: int
    user_override_count: int
    updated_by: uuid.UUID | None
    updated_by_email: str | None
    updated_at: datetime | None


class AdminInstructionUpdateRequest(BaseModel):
    """管理员保存平台默认指令：``content`` 必填但可显式 null。"""

    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(
        description="平台默认正文；null 或 strip 后空串表示恢复内置默认"
    )

    @field_validator("content")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class AdminSkillStats(BaseModel):
    """系统技能统计：基于当前筛选结果；``enabled`` 为全局有效启用数。"""

    total: int
    enabled: int
    force_disabled: int


class AdminSkillListOut(BaseModel):
    """管理端系统技能列表；系统技能数量有上界，不分页（R6）。"""

    items: list[SkillOut]
    stats: AdminSkillStats


class AdminSkillForceDisableRequest(BaseModel):
    """管理员强制停用/恢复系统技能。"""

    model_config = ConfigDict(extra="forbid")

    force_disabled: bool


class AdminOverviewUsers(BaseModel):
    """全表用户计数；``admins`` 统计 ``role='admin'`` 的所有状态行。"""

    total: int
    active: int
    disabled: int
    admins: int


class AdminOverviewConversations(BaseModel):
    """全表会话计数（含软删）；``deleted`` 为 ``status='deleted'`` 的行数。"""

    total: int
    active: int
    archived: int
    deleted: int


class AdminOverviewMessages(BaseModel):
    total: int


class AdminOverviewRuns(BaseModel):
    """全表 run 计数（含失败/取消/中断等终态）。"""

    total: int
    queued: int
    running: int
    succeeded: int
    failed: int
    canceled: int
    interrupted: int


class AdminOverviewA2aTasks(BaseModel):
    """全表 A2A 任务计数（含终态），状态键与模型枚举一一对应。"""

    total: int
    submitted: int
    working: int
    input_required: int
    auth_required: int
    completed: int
    failed: int
    canceled: int
    rejected: int


class AdminOverviewKnowledge(BaseModel):
    """知识库文档/分块总数与文档状态分布。"""

    documents: int
    chunks: int
    documents_pending: int
    documents_indexing: int
    documents_ready: int
    documents_failed: int


class AdminOverviewAttachments(BaseModel):
    """附件计数；``total``/``storage_bytes`` 只计未删除行，``deleted`` 单独计数。"""

    total: int
    storage_bytes: int
    deleted: int


class AdminOverviewOut(BaseModel):
    """管理台只读概览。

    计数口径：``users``/``conversations``/``messages``/``runs``/``a2a_tasks``/
    ``knowledge`` 为全表原始计数（含软删/失败/终态），``conversations.deleted``
    单列；``attachments.total`` 只计 ``status != 'deleted'`` 的行，
    ``storage_bytes`` 为这些行的 ``SUM(size_bytes)``，``deleted`` 单独计数；
    所有状态键始终存在，无数据时为 ``0``。

    性能口径：精确计数，O(表大小)；管理端低频只读；表规模增大后再评估统计表，
    不在 M8。
    """

    generated_at: datetime
    users: AdminOverviewUsers
    conversations: AdminOverviewConversations
    messages: AdminOverviewMessages
    runs: AdminOverviewRuns
    a2a_tasks: AdminOverviewA2aTasks
    knowledge: AdminOverviewKnowledge
    attachments: AdminOverviewAttachments
