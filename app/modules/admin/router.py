"""管理台路由：用户管理、只读概览、平台默认指令、系统技能、系统 MCP 与全局 Agent。

本文件是 M8 管理台路由骨架（``prefix="/admin"``）。鉴权统一走 M7 的
``AdminUser``，403 信封由 ``require_admin`` 产生，不在这里重复判断角色。管理端是
配置面：能力总开关（``INSTRUCTIONS_ENABLED``/``SKILLS_ENABLED``/``MCP_ENABLED``）
关闭时这些端点仍可读写（R1）。
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Query

from app.api.schemas import ErrorEnvelopeOut
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
from app.modules.agents import service as agents_service
from app.modules.agents.schemas import (
    AdminAgentUpdateRequest,
    AgentDetailOut,
    AgentPage,
)
from app.modules.mcp import service as mcp_service
from app.modules.mcp.schemas import (
    AdminMcpServerUpdateRequest,
    McpServerCreateRequest,
    McpServerListOut,
    McpServerOut,
)
from app.modules.skills import service as skills_service
from app.modules.skills.schemas import SkillEnabledRequest, SkillOut

router = APIRouter(prefix="/admin", tags=["管理台"])

# 管理台所有端点统一 403 语义（覆盖 include 级通用 403 描述）。
_ADMIN_RESPONSES: dict[int | str, dict] = {
    403: {
        "model": ErrorEnvelopeOut,
        "description": "需要管理员权限（`forbidden`，`details.required_role=\"admin\"`）",
    }
}


@router.get(
    "/users",
    response_model=AdminUserPage,
    summary="管理员分页查询用户",
    description=(
        "按创建时间倒序返回用户。`q` 模糊匹配邮箱或展示名（大小写不敏感），"
        "`role`/`status` 为枚举过滤；`stats` 是当前筛选条件下的全量计数。"
    ),
    responses=_ADMIN_RESPONSES,
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
    responses=_ADMIN_RESPONSES,
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
    responses=_ADMIN_RESPONSES,
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
    responses=_ADMIN_RESPONSES,
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
    responses=_ADMIN_RESPONSES,
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
        "no-op，不写审计）；非空时写入 `{\"content\": ...}`，长度超过 "
        "`instruction_max_chars` 返回 422 `instruction_too_long`。真实写入/恢复写 "
        "`instruction.update` 审计，`detail` 只含 `scope`/长度/指纹，不含正文。"
        "`INSTRUCTIONS_ENABLED=false` 时"
        "仍可写（R1）；响应与 GET 同形。"
    ),
    responses=_ADMIN_RESPONSES,
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
    responses=_ADMIN_RESPONSES,
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
        "真实变更写入全局 `skills.enabled` 并写 `skill.enable` 审计"
        "（`detail={\"scope\": \"system\", \"enabled\": ...}`）；同值启停是 "
        "no-op，不写审计。响应里的 "
        "`effective_enabled = enabled and not force_disabled`。"
        "`SKILLS_ENABLED=false` 时仍可写（R1）。"
    ),
    responses=_ADMIN_RESPONSES,
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
        "`force_disabled=true` 会覆盖任何用户级覆盖，使技能对所有人失效；真实变更写 "
        "`skill.force_disable` 审计（`detail={\"force_disabled\": ...}`），"
        "同值启停是 no-op，不写审计。"
        "`SKILLS_ENABLED=false` 时仍可写（R1）。"
    ),
    responses=_ADMIN_RESPONSES,
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


@router.get(
    "/mcp/servers",
    response_model=McpServerListOut,
    summary="管理员查询系统 MCP 服务器",
    description=(
        "只返回 `scope='system'` 的 MCP 服务器，按创建时间倒序；`stats` 与用户侧"
        "同形（`user=0`、`total==system==len(items)`），`enabled` 按全局口径 "
        "`enabled and not force_disabled` 计数，不合并任何用户覆盖。系统 MCP 数量"
        "有上界，因此不分页。该端点属于管理端配置面，`MCP_ENABLED=false` 时仍可读"
        "（R1）。"
    ),
    responses=_ADMIN_RESPONSES,
)
async def list_system_mcp_servers(
    admin: AdminUser, db: DbSession
) -> McpServerListOut:
    return await mcp_service.list_system_servers(db)


@router.post(
    "/mcp/servers",
    response_model=McpServerOut,
    status_code=201,
    summary="管理员创建系统 MCP 服务器",
    description=(
        "复用用户侧校验：stdio 仅管理员、URL 做 SSRF 校验、command 必填；请求头里的"
        "`secret:` 平台密钥引用可用，敏感头只落引用/密文。slug 在任意 scope 已存在时"
        "返回 409 `conflict`（系统 MCP 工具名 `mcp__<slug>__*` 必须全局唯一）。创建后"
        "立即做一次工具发现：可达则同步工具并标 healthy，不可达仍返回 201、"
        "`health_status='unhealthy'` 且保留截断后的 `last_error`。写"
        "`mcp.server.create` 审计，detail 只含 slug/transport/url/scope。"
        "`MCP_ENABLED=false` 时仍可写（R1）。"
    ),
    responses=_ADMIN_RESPONSES,
)
async def create_system_mcp_server(
    payload: McpServerCreateRequest, admin: AdminUser, db: DbSession
) -> McpServerOut:
    return await mcp_service.create_system_server(db, admin, payload)


@router.patch(
    "/mcp/servers/{server_id}",
    response_model=McpServerOut,
    summary="管理员更新系统 MCP 服务器",
    description=(
        "只允许修改 `name`/`description`/`url`/`headers`/`auto_inject`/`enabled`/"
        "`force_disabled`，至少提交一项；`slug`/`transport`/`command`/`args` 为创建时"
        "确定的只读字段，提交会显式 422。`url` 走 SSRF 校验，`headers` 支持 `secret:`"
        "平台密钥引用。真实变更写 `mcp.server.update` 审计（detail 只含字段名与 scope）"
        "并强制重新发现工具；同值 PATCH 直接返回当前视图，不写审计、不触发发现。"
        "`MCP_ENABLED=false` 时仍可写（R1）。"
    ),
    responses=_ADMIN_RESPONSES,
)
async def update_system_mcp_server(
    server_id: uuid.UUID,
    payload: AdminMcpServerUpdateRequest,
    admin: AdminUser,
    db: DbSession,
) -> McpServerOut:
    return await mcp_service.update_system_server(db, admin, server_id, payload)


@router.delete(
    "/mcp/servers/{server_id}",
    status_code=204,
    summary="管理员删除系统 MCP 服务器",
    description=(
        "只允许删除系统 MCP；不存在返回 404 `not_found`。先清理该服务器的用户级"
        "启停覆盖，工具行随外键级联删除，并写 `mcp.server.delete` 审计"
        "（detail 只含 slug/scope）。`MCP_ENABLED=false` 时仍可写（R1）。"
    ),
    responses=_ADMIN_RESPONSES,
)
async def delete_system_mcp_server(
    server_id: uuid.UUID, admin: AdminUser, db: DbSession
) -> None:
    await mcp_service.delete_system_server(db, admin, server_id)


@router.get(
    "/agents",
    response_model=AgentPage,
    summary="管理员查询全局 Agent 注册表",
    description=(
        "查询参数与 `GET /agents` 完全一致：`health_status`、`enabled`、`q`、`limit`、"
        "`cursor`。按 v1 全局注册表口径返回所有未软删 Agent，不按 `owner_user_id` "
        "过滤（R8：`owner_user_id` 仅为保留字段，不参与可见性）。"
    ),
    responses=_ADMIN_RESPONSES,
)
async def list_admin_agents(
    admin: AdminUser,
    db: DbSession,
    health_status: str | None = Query(default=None, max_length=16),
    enabled: bool | None = Query(default=None),
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=500),
) -> AgentPage:
    return await agents_service.list_agents(
        db,
        health_status=health_status,
        enabled=enabled,
        q=q,
        limit=limit,
        cursor=cursor,
        stats_ignore_cursor=True,
    )


@router.patch(
    "/agents/{agent_id}",
    response_model=AgentDetailOut,
    summary="管理员更新全局 Agent",
    description=(
        "只允许修改 `name`/`enabled`/`requires_approval`/`auth`/`headers`，至少提交"
        "一项；`slug`/`url`/`description`/`protocol_version`/`card`/`skills`/"
        "`capabilities`/`health_status`/`owner_user_id` 为只读字段，提交会显式 422。"
        "改名会置 `name_edited=true`，后续 refresh 不再用 Card 覆盖；`auth` 支持 "
        "`secret:` 平台密钥引用。真实变更写 `agent.updated` 审计"
        "（detail={\"fields\": [...], \"source\": \"admin\"}）；同值 PATCH 不写审计。"
        "不存在或已软删返回 404 `not_found`。"
    ),
    responses=_ADMIN_RESPONSES,
)
async def update_admin_agent(
    agent_id: uuid.UUID,
    payload: AdminAgentUpdateRequest,
    admin: AdminUser,
    db: DbSession,
) -> AgentDetailOut:
    return await agents_service.admin_update_agent(db, admin, agent_id, payload)
