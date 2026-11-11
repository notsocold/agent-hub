"""管理台路由：用户管理、只读概览、平台默认指令与系统技能管理。

本文件是 M8 管理台路由骨架（``prefix="/admin"``）；后续任务在同一文件追加
系统 MCP、全局 Agent 端点。鉴权统一走 M7 的 ``AdminUser``，403 信封由
``require_admin`` 产生，不在这里重复判断角色。管理端是配置面：能力总开关
（``INSTRUCTIONS_ENABLED``/``SKILLS_ENABLED``）关闭时这些端点仍可读写（R1）。
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Query

from app.core.deps import AdminUser, DbSession
from app.modules.admin import service
from app.modules.admin.schemas import (
    AdminInstructionOut,
    AdminInstructionUpdateRequest,
    AdminOverviewOut,
    AdminSkillForceDisableRequest,
    AdminSkillListOut,
    AdminSkillStats,
    AdminUserOut,
    AdminUserPage,
    AdminUserPatchRequest,
)
from app.modules.skills import service as skills_service
from app.modules.skills.schemas import SkillEnabledRequest, SkillOut

router = APIRouter(prefix="/admin", tags=["管理台"])


@router.get(
    "/users",
    response_model=AdminUserPage,
    summary="管理员分页查询用户",
    description=(
        "按创建时间倒序返回用户。`q` 模糊匹配邮箱或展示名（大小写不敏感），"
        "`role`/`status` 为枚举过滤；`stats` 是当前筛选条件下的全量计数。"
    ),
)
async def list_users(
    admin: AdminUser,
    db: DbSession,
    q: str | None = Query(default=None, max_length=100, description="邮箱或展示名片段"),
    role: Literal["user", "admin"] | None = Query(default=None, description="角色过滤"),
    status_filter: Literal["active", "disabled"] | None = Query(
        default=None, alias="status", description="状态过滤"
    ),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=500),
) -> AdminUserPage:
    return await service.list_users(
        db, q=q, role=role, status=status_filter, limit=limit, cursor=cursor
    )


@router.get(
    "/users/{user_id}",
    response_model=AdminUserOut,
    summary="管理员查看用户详情",
    description="返回与列表项相同形状的用户字段；响应永不包含密码散列。",
)
async def get_user(
    user_id: uuid.UUID, admin: AdminUser, db: DbSession
) -> AdminUserOut:
    return await service.get_user(db, user_id)


@router.patch(
    "/users/{user_id}",
    response_model=AdminUserOut,
    summary="管理员修改用户角色或状态",
    description=(
        "只允许修改 `role`/`status`，且至少提交一项；不能修改自己的角色、"
        "也不能停用自己；移除最后一个活跃管理员返回 409 `last_admin_protected`。"
        "真实变更写 `admin.user.update` 审计，同值 PATCH 直接返回当前值。"
    ),
)
async def update_user(
    user_id: uuid.UUID,
    payload: AdminUserPatchRequest,
    admin: AdminUser,
    db: DbSession,
) -> AdminUserOut:
    return await service.update_user(db, admin, user_id, payload)


@router.get(
    "/overview",
    response_model=AdminOverviewOut,
    summary="管理员查看只读概览",
    description=(
        "实时返回用户、会话、消息、运行、A2A 任务、知识库与附件的全表计数。"
        "``users``/``conversations``/``messages``/``runs``/``a2a_tasks``/``knowledge`` "
        "含软删/失败/终态行；``conversations.deleted`` 单列；``attachments`` 只计"
        "未删除行，``deleted`` 单独计数。所有状态键始终存在，无数据时为 0。"
        "该接口只读、不写审计，每次请求执行 8 条聚合查询。"
    ),
)
async def get_overview(admin: AdminUser, db: DbSession) -> AdminOverviewOut:
    return await service.build_overview(db)


@router.get(
    "/settings/instruction",
    response_model=AdminInstructionOut,
    summary="管理员读取平台默认指令",
    description=(
        "返回平台默认指令正文与元信息。`is_default=true` 表示没有 "
        "`system_settings` 覆盖、正在使用代码内置默认；`user_override_count` "
        "统计 `user_settings.instructions IS NOT NULL` 的用户数。平台默认文本"
        "本身就是无个人覆盖用户实际注入的内容，因此不返回同义的 "
        "`effective_preview`。该端点属于管理端配置面，`INSTRUCTIONS_ENABLED=false` "
        "时仍可读（R1）。"
    ),
)
async def get_platform_instruction(
    admin: AdminUser, db: DbSession
) -> AdminInstructionOut:
    return await service.get_admin_instruction(db)


@router.put(
    "/settings/instruction",
    response_model=AdminInstructionOut,
    summary="管理员保存或恢复平台默认指令",
    description=(
        "`content` 必填但可显式传 null：null 或 strip 后空串会删除 "
        "`system_settings['instruction.default']` 覆盖并恢复代码内置默认（无行时 "
        "no-op）；非空时写入 `{\"content\": ...}`，长度超过 `instruction_max_chars` "
        "返回 422 `instruction_too_long`。保存与恢复都写 `instruction.update` 审计，"
        "`detail` 只含 `scope`/长度/指纹，不含正文。`INSTRUCTIONS_ENABLED=false` 时"
        "仍可写（R1）；响应与 GET 同形。"
    ),
)
async def update_platform_instruction(
    payload: AdminInstructionUpdateRequest,
    admin: AdminUser,
    db: DbSession,
) -> AdminInstructionOut:
    return await service.update_admin_instruction(db, admin, payload.content)


async def _admin_skill_list(db: DbSession, *, q: str | None) -> AdminSkillListOut:
    """把系统技能列表装配成管理端视图；stats 基于当前筛选结果。"""
    items = await skills_service.list_system_skills(db, q=q)
    return AdminSkillListOut(
        items=items,
        stats=AdminSkillStats(
            total=len(items),
            enabled=sum(1 for item in items if item.effective_enabled),
            force_disabled=sum(1 for item in items if item.force_disabled),
        ),
    )


@router.get(
    "/skills",
    response_model=AdminSkillListOut,
    summary="管理员查询系统技能",
    description=(
        "只返回 `scope='system'` 的技能，按名称升序；`q` 大小写不敏感地模糊匹配"
        "名称或描述（最多 100 字符，LIKE 通配符会被转义）。`effective_enabled` 是"
        "全局口径 `enabled and not force_disabled`，不合并任何用户覆盖；系统技能"
        "数量有上界，因此不分页（R6）。`stats` 为当前筛选结果内的 "
        "total/enabled/force_disabled。该端点属于管理端配置面，"
        "`SKILLS_ENABLED=false` 时仍可读（R1）。"
    ),
)
async def list_system_skills(
    admin: AdminUser,
    db: DbSession,
    q: str | None = Query(default=None, max_length=100, description="名称或描述片段"),
) -> AdminSkillListOut:
    return await _admin_skill_list(db, q=q)


@router.put(
    "/skills/{skill_id}/enabled",
    response_model=SkillOut,
    summary="管理员启停系统技能",
    description=(
        "只允许操作系统技能；不存在或属于用户私有技能返回 404 `not_found`。"
        "写入全局 `skills.enabled` 并写 `skill.enable` 审计"
        "（`detail={\"scope\": \"system\", \"enabled\": ...}`），响应里的 "
        "`effective_enabled = enabled and not force_disabled`。"
        "`SKILLS_ENABLED=false` 时仍可写（R1）。"
    ),
)
async def set_system_skill_enabled(
    skill_id: uuid.UUID,
    payload: SkillEnabledRequest,
    admin: AdminUser,
    db: DbSession,
) -> SkillOut:
    return await skills_service.set_system_skill_enabled(
        db, admin, skill_id, payload.enabled
    )


@router.put(
    "/skills/{skill_id}/force-disable",
    response_model=SkillOut,
    summary="管理员强制停用或恢复系统技能",
    description=(
        "只允许操作系统技能；不存在或属于用户私有技能返回 404 `not_found`。"
        "`force_disabled=true` 会覆盖任何用户级覆盖，使技能对所有人失效；写 "
        "`skill.force_disable` 审计（`detail={\"force_disabled\": ...}`）。"
        "`SKILLS_ENABLED=false` 时仍可写（R1）。"
    ),
)
async def set_system_skill_force_disabled(
    skill_id: uuid.UUID,
    payload: AdminSkillForceDisableRequest,
    admin: AdminUser,
    db: DbSession,
) -> SkillOut:
    return await skills_service.set_system_skill_force_disabled(
        db, admin, skill_id, payload.force_disabled
    )
