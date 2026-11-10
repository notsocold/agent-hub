"""聊天能力装配：附件快照、指令、记忆、技能与 MCP 的运行时合并。

装配结果每轮生成，不写 checkpoint；账号级配置修改下一轮立即生效。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.prompts import (
    assemble_system_prompt,
    format_memory_line,
    truncate_to_tokens,
)
from app.core.config import get_settings
from app.core.errors import CapabilityDisabledError, NotFoundError, ValidationFailedError
from app.db.models.attachment import Attachment
from app.db.models.knowledge import KnowledgeDocument
from app.db.models.skill import Skill
from app.db.models.user import User
from app.modules.chat.schemas import ChatStreamRequest
from app.modules.instructions import service as instructions_service
from app.modules.knowledge.repository import visible_filter
from app.modules.mcp.service import ChatMcpTool, collect_chat_tools
from app.modules.memories import service as memories_service
from app.modules.skills import service as skills_service


@dataclass
class AssembledCapabilities:
    system_prompt: str = ""
    tools: list[Any] = field(default_factory=list)
    mcp_tool_map: dict[str, ChatMcpTool] = field(default_factory=dict)
    a2a_agent_map: dict[str, Any] = field(default_factory=dict)
    skill_activations: list[dict[str, Any]] = field(default_factory=list)
    notices: list[dict[str, str]] = field(default_factory=list)
    attachment_snapshot: list[dict[str, Any]] = field(default_factory=list)
    attachment_text: str = ""
    active_skill_ids: set[str] = field(default_factory=set)


async def validate_chat_selections(db: AsyncSession, user: User, payload: ChatStreamRequest) -> None:
    """start_chat 阶段的快速校验：资源必须存在、归属本人、当前可用。

    完整装配在 execute_run 里进行；这里只把 4xx 提前到 HTTP 层。
    """
    settings = get_settings()
    if payload.attachments:
        if not settings.attachments_enabled:
            raise CapabilityDisabledError("附件能力未启用")
        await load_attachments(db, user, payload.attachments)
    if payload.skills:
        if not settings.skills_enabled:
            raise CapabilityDisabledError("技能能力未启用")
        await validate_skill_ids(db, user, payload.skills)
    if payload.mcp_servers or payload.mcp_tools:
        if not settings.mcp_enabled:
            raise CapabilityDisabledError("MCP 能力未启用")
        await collect_chat_tools(
            db,
            user,
            explicit_servers=list(payload.mcp_servers),
            explicit_tools=list(payload.mcp_tools),
        )
    # knowledge_categories 不在此校验：启用门禁只有 start_chat 的 _check_capabilities
    # 一处；开关开启时分类直接随请求写入 run.capabilities，作为 search_knowledge 的
    # 默认过滤条件（显式工具参数优先）。


async def _has_visible_ready_documents(db: AsyncSession, user: User) -> bool:
    """当前用户是否可见至少一篇 ``ready`` 文档（系统文档 + 本人文档）。

    可见性口径复用检索侧的 :func:`visible_filter`，避免提示词门禁与检索范围漂移。
    """
    stmt = (
        select(KnowledgeDocument.id)
        .where(KnowledgeDocument.status == "ready", *visible_filter(user))
        .limit(1)
    )
    return await db.scalar(stmt) is not None


async def load_attachments(
    db: AsyncSession, user: User, attachment_ids: list[uuid.UUID]
) -> list[Attachment]:
    """校验附件归属与状态，返回可用附件；越权/不存在 404，未就绪 422。"""
    seen: set[uuid.UUID] = set()
    attachments: list[Attachment] = []
    for attachment_id in attachment_ids:
        if attachment_id in seen:
            continue
        seen.add(attachment_id)
        attachment = await db.get(Attachment, attachment_id)
        if attachment is None or attachment.user_id != user.id or attachment.status == "deleted":
            raise NotFoundError("附件不存在")
        if attachment.status != "ready":
            raise ValidationFailedError(
                f"附件「{attachment.filename}」尚未就绪", code="attachment_not_ready"
            )
        attachments.append(attachment)
    return attachments


async def validate_skill_ids(db: AsyncSession, user: User, skill_ids: list[uuid.UUID]) -> None:
    settings = get_settings()
    total_tokens = 0
    for skill_id in skill_ids:
        skill = await db.get(Skill, skill_id)
        if skill is None or (skill.scope == "user" and skill.owner_user_id != user.id):
            raise NotFoundError("所选技能不存在")
        effective = await skills_service._effective_enabled(db, skill, user)
        if not effective:
            raise ValidationFailedError(
                f"技能「{skill.name}」已停用", code="capability_disabled"
            )
        total_tokens += skill.estimated_tokens
    if total_tokens > settings.skill_max_body_tokens:
        raise ValidationFailedError(
            f"本轮激活技能正文约 {total_tokens} tokens，超过 {settings.skill_max_body_tokens} 上限",
            code="skill_too_large",
        )


async def assemble(
    db: AsyncSession,
    user: User,
    *,
    attachment_ids: list[uuid.UUID],
    skill_ids: list[uuid.UUID],
    mcp_server_ids: list[uuid.UUID],
    mcp_tool_ids: list[uuid.UUID],
) -> AssembledCapabilities:
    """execute_run 阶段装配：系统提示词 + 工具目录 + 事件负载。"""
    settings = get_settings()
    result = AssembledCapabilities()

    # 1. 附件：快照 + 文本内容注入
    attachments = await load_attachments(db, user, attachment_ids)
    result.attachment_snapshot = [
        {
            "id": str(item.id),
            "filename": item.filename,
            "mime_type": item.mime_type,
            "size_bytes": item.size_bytes,
        }
        for item in attachments
    ]
    text_parts: list[str] = []
    for item in attachments:
        if item.text_excerpt:
            excerpt = truncate_to_tokens(item.text_excerpt, 4000)
            text_parts.append(f"[附件：{item.filename}]\n{excerpt}")
    result.attachment_text = "\n\n".join(text_parts)

    # 2. 指令
    instruction = await instructions_service.effective_instruction_text(db, user)

    # 3. 记忆（置顶优先，token 预算截断）
    memory_lines: list[str] = []
    if settings.memories_enabled:
        memories = await memories_service.active_memories_for_injection(
            db, user, settings.memory_max_tokens
        )
        memory_lines = [
            format_memory_line(item.kind, item.pinned, item.content) for item in memories
        ]

    # 4. 技能：目录 + 显式激活正文
    catalog_lines: list[str] = []
    active_skills: list[tuple[str, str]] = []
    result.active_skill_ids = {str(item) for item in skill_ids}
    if settings.skills_enabled:
        visible = list(
            (
                await db.execute(
                    select(Skill).where(
                        (Skill.scope == "system") | (Skill.owner_user_id == user.id)
                    )
                )
            ).scalars()
        )
        for skill in visible:
            if not await skills_service._effective_enabled(db, skill, user):
                continue
            catalog_lines.append(f"- {skill.slug}: {skill.description[:200]}")
            if str(skill.id) in result.active_skill_ids:
                active_skills.append((skill.name, skill.content))
                result.skill_activations.append(
                    {
                        "skill_id": str(skill.id),
                        "slug": skill.slug,
                        "name": skill.name,
                        "scope": skill.scope,
                        "estimated_tokens": skill.estimated_tokens,
                    }
                )
        catalog_lines = catalog_lines[: settings.skill_catalog_max_items]
        catalog_text = truncate_to_tokens("\n".join(catalog_lines), 1000)
    else:
        catalog_text = ""

    # 5. MCP 工具目录
    mcp_tools, mcp_notices = await collect_chat_tools(
        db,
        user,
        explicit_servers=list(mcp_server_ids),
        explicit_tools=list(mcp_tool_ids),
    )
    result.mcp_tool_map = {tool.exposed_name: tool for tool in mcp_tools}
    result.notices.extend(mcp_notices)
    tool_notes = ""
    if mcp_server_ids or mcp_tool_ids:
        chosen = [tool.exposed_name for tool in mcp_tools]
        tool_notes = "用户本轮显式选择了部分 MCP 能力，优先使用：" + "、".join(chosen[:20]) + "。"

    # 5.5 A2A 目录：enabled 且健康的远程 Agent 注入统一 send_message 工具。
    from app.modules.agents import service as agents_service

    agents = await agents_service.enabled_agents_for_chat(db)
    result.a2a_agent_map = {agent.slug: agent for agent in agents}
    if agents:
        catalog: list[str] = []
        for agent in agents:
            skills = "、".join(
                str(skill.get("name") or "") for skill in (agent.skills or []) if isinstance(skill, dict)
            )
            catalog.append(
                f"- {agent.slug}（{agent.name}）{'：' + agent.description[:120] if agent.description else ''}"
                + (f"；技能：{skills}" if skills else "")
            )
        tool_notes = (
            (tool_notes + "；" if tool_notes else "")
            + "可委派的远程智能体（send_message 工具的 agent 取值）："
            + "；".join(catalog[:10])
            + "。"
        )

    # 6. 知识库规则：能力开启 + 当前用户可见 ready 文档时才注入（空库不提示）。
    knowledge_available = False
    if settings.knowledge_enabled:
        knowledge_available = await _has_visible_ready_documents(db, user)

    # 7. 静态提示词预算：先丢低分记忆，再截技能目录。
    prompt = assemble_system_prompt(
        instruction=instruction,
        memory_lines=memory_lines,
        skill_catalog=catalog_text,
        active_skills=active_skills,
        tool_notes=tool_notes,
        knowledge_available=knowledge_available,
    )
    if len(prompt) > settings.prompt_budget_tokens:
        while memory_lines and len(prompt) > settings.prompt_budget_tokens:
            memory_lines.pop()
            prompt = assemble_system_prompt(
                instruction=instruction,
                memory_lines=memory_lines,
                skill_catalog=catalog_text,
                active_skills=active_skills,
                tool_notes=tool_notes,
                knowledge_available=knowledge_available,
            )
    result.system_prompt = prompt
    return result
