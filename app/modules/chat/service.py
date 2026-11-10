"""聊天与运行业务：创建 run、执行图、事件流、取消与查询。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.messages import HumanMessage
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.checkpointer import open_checkpointer
from app.agents.graph import build_graph
from app.agents.models import build_chat_model
from app.core.config import get_settings
from app.core.cursor import encode_cursor
from app.core.errors import (
    CapabilityDisabledError,
    ConflictError,
    NotFoundError,
    RunInProgressError,
    RunNotCancelableError,
    ValidationFailedError,
    current_request_id,
)
from app.core.timeutil import elapsed_ms
from app.db.models.approval import Approval
from app.db.models.chat import Run, RunEvent
from app.db.models.conversation import Conversation, Message
from app.db.models.remote import A2aTask, RemoteAgent
from app.db.models.user import User
from app.db.session import get_session_factory
from app.modules.chat import capabilities as capabilities_module
from app.modules.chat.bus import bus
from app.modules.chat.capabilities import validate_chat_selections
from app.modules.chat.events import (
    TERMINAL_EVENTS,
    RunEventPublisher,
    format_sse,
    to_iso,
)
from app.modules.chat.registry import RunContext, registry
from app.modules.chat.schemas import (
    ChatStartResult,
    ChatStreamRequest,
    RunAgentOut,
    RunCancelResult,
    RunEventOut,
    RunEventPage,
    RunMessageIdsOut,
    RunOut,
    RunPendingOut,
    RunResumeResult,
)
from app.modules.chat.tools import (
    ApprovalTimeoutError,
    ToolRunner,
    build_mcp_tools,
    build_native_tools,
    build_search_knowledge_tool,
    build_send_message_tool,
)
from app.modules.conversations.schemas import (
    MessageCapabilitiesOut,
    MessageErrorOut,
    UsageOut,
)

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = ("queued", "running", "interrupted")
_DEFAULT_CAPABILITIES: dict[str, Any] = {
    "skills": [],
    "mcp_servers": [],
    "mcp_tools": [],
    "knowledge_categories": [],
    "workflow_id": None,
}


def _now() -> datetime:
    return datetime.now(UTC)


def _raise_missing_run_data() -> None:
    """运行关联数据缺失属于服务端状态异常，统一在后台任务里转成失败事件。"""
    raise RuntimeError("运行关联数据不存在")


def _title_from_message(message: str) -> str:
    text = " ".join(message.split())
    return (text[:40] or "新对话")[:200]


def _normalize_capabilities(raw: dict[str, Any] | None) -> MessageCapabilitiesOut:
    data = {**_DEFAULT_CAPABILITIES, **(raw or {})}
    return MessageCapabilitiesOut(
        skills=[str(item) for item in data.get("skills", [])],
        mcp_servers=[str(item) for item in data.get("mcp_servers", [])],
        mcp_tools=[str(item) for item in data.get("mcp_tools", [])],
        knowledge_categories=[str(item) for item in data.get("knowledge_categories", [])],
        workflow_id=data.get("workflow_id"),
    )


def _usage_dict(raw: dict[str, Any] | None) -> dict[str, int]:
    raw = raw or {}
    input_tokens = int(raw.get("input_tokens") or 0)
    output_tokens = int(raw.get("output_tokens") or 0)
    reasoning_tokens = int(raw.get("reasoning_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": int(raw.get("total_tokens") or input_tokens + output_tokens),
    }


def _usage_out(raw: dict[str, Any] | None) -> UsageOut:
    data = _usage_dict(raw)
    return UsageOut(**data)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def _check_capabilities(payload: ChatStreamRequest) -> None:
    """尚未实现的能力显式拒绝；已实现的能力走归属/启用校验。

    ``knowledge_categories`` 的启用门禁（唯一一处）：开关关闭时 403，
    开启时直接放行（分类只作为 search_knowledge 的默认过滤条件）。
    """
    if payload.knowledge_categories and not get_settings().knowledge_enabled:
        raise CapabilityDisabledError("知识库能力未启用")
    if payload.workflow_id is not None:
        raise ConflictError("工作流能力尚未启用", code="workflow_disabled")


async def _load_owned_run(db: AsyncSession, user: User, run_id: uuid.UUID) -> Run:
    run = await db.get(Run, run_id)
    if run is None:
        raise NotFoundError("运行不存在")
    conversation = await db.get(Conversation, run.conversation_id)
    if conversation is None or conversation.user_id != user.id:
        raise NotFoundError("运行不存在")
    return run


async def _find_active_run(db: AsyncSession, conversation_id: uuid.UUID) -> Run | None:
    return await db.scalar(
        select(Run)
        .where(Run.conversation_id == conversation_id, Run.status.in_(_ACTIVE_STATUSES))
        .order_by(Run.created_at.desc())
        .limit(1)
    )


async def start_chat(db: AsyncSession, user: User, payload: ChatStreamRequest) -> ChatStartResult:
    settings = get_settings()
    model = payload.model or settings.llm_model
    if model not in settings.allowed_models:
        raise ValidationFailedError("该模型不在允许列表中", code="model_not_allowed")
    manual_context = bool(
        payload.skills or payload.mcp_servers or payload.mcp_tools or payload.knowledge_categories
    )
    if payload.workflow_id is not None and manual_context:
        raise ValidationFailedError(
            "工作流与手动能力上下文不能同时使用", code="workflow_context_conflict"
        )
    if payload.workflow_id is None and payload.workflow_version is not None:
        raise ValidationFailedError("workflow_version 不能脱离 workflow_id 使用")
    if payload.reasoning_effort not in {None, "low", "medium", "high"}:
        raise ValidationFailedError("推理等级不合法", code="reasoning_effort_invalid")
    _check_capabilities(payload)
    await validate_chat_selections(db, user, payload)

    reasoning_effort = payload.reasoning_effort or "low"
    capabilities = {
        "skills": [str(item) for item in payload.skills],
        "mcp_servers": [str(item) for item in payload.mcp_servers],
        "mcp_tools": [str(item) for item in payload.mcp_tools],
        "attachments": [str(item) for item in payload.attachments],
        "knowledge_categories": list(payload.knowledge_categories),
        "workflow_id": str(payload.workflow_id) if payload.workflow_id else None,
    }

    is_new = payload.conversation_id is None
    if is_new:
        conversation = Conversation(user_id=user.id, title=_title_from_message(payload.message))
        db.add(conversation)
        await db.flush()
    else:
        conversation = await db.scalar(
            select(Conversation)
            .where(
                Conversation.id == payload.conversation_id,
                Conversation.user_id == user.id,
                Conversation.status != "deleted",
            )
            .with_for_update()
        )
        if conversation is None:
            raise NotFoundError("会话不存在")
        if conversation.status == "archived":
            raise ConflictError("会话已归档，无法继续发送消息")

    # 幂等：同一个 client_message_id 直接复用已有 run，返回事件回放。
    if payload.client_message_id:
        existing = await db.scalar(
            select(Run).where(
                Run.conversation_id == conversation.id,
                Run.idempotency_key == payload.client_message_id,
            )
        )
        if existing is not None:
            await db.commit()
            return ChatStartResult(run_id=existing.id, conversation_id=conversation.id, replay=True)

    active = await _find_active_run(db, conversation.id)
    if active is not None:
        raise RunInProgressError(str(active.id))

    now = _now()
    # 两条消息在同一事务里创建，显式错开 1 微秒，保证历史分页顺序稳定。
    user_message = Message(
        conversation_id=conversation.id,
        role="user",
        content=payload.message,
        content_type="text",
        client_message_id=payload.client_message_id,
        status="completed",
        # 与助手消息一致：用户消息同样落 capabilities，历史时间线依赖它展示
        # 「本轮：技能 ×N · MCP ×N」；附件另有 attachments 快照字段。
        capabilities=capabilities,
        created_at=now,
    )
    assistant_message = Message(
        conversation_id=conversation.id,
        role="assistant",
        content="",
        content_type="markdown",
        status="streaming",
        capabilities=capabilities,
        created_at=now + timedelta(microseconds=1),
    )
    db.add_all([user_message, assistant_message])
    await db.flush()

    run = Run(
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        assistant_message_id=assistant_message.id,
        source="user",
        status="running",
        model=model,
        reasoning_effort=reasoning_effort,
        capabilities=capabilities,
        idempotency_key=payload.client_message_id,
        started_at=now,
    )
    db.add(run)
    await db.flush()
    user_message.run_id = run.id
    assistant_message.run_id = run.id

    conversation.last_message_at = now
    conversation.last_message_preview = payload.message[:80]
    conversation.message_count += 1
    if is_new or conversation.title == "新对话":
        conversation.title = _title_from_message(payload.message)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        active = await _find_active_run(db, conversation.id)
        if active is not None:
            raise RunInProgressError(str(active.id)) from exc
        raise ConflictError("发送消息冲突，请重试") from exc

    publisher = RunEventPublisher(run.id, get_session_factory(), bus)
    context = RunContext(run_id=run.id, publisher=publisher)
    registry.register(context)
    registry.start(context, execute_run(context, is_new))
    return ChatStartResult(run_id=run.id, conversation_id=conversation.id, replay=False)


async def execute_run(context: RunContext, is_new_conversation: bool) -> None:
    """后台运行：独立于 SSE 连接，客户端断开不影响执行。"""
    settings = get_settings()
    run_id = context.run_id
    publisher = context.publisher
    started_wall = _now()
    started_at: datetime | None = None
    first_token_ms: int | None = None
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    reasoning_started: datetime | None = None
    usage: dict[str, Any] = {}

    try:
        async with get_session_factory()() as db:
            run = await db.get(Run, run_id)
            if run is None:
                return
            if run.status != "running":
                # 取消可能发生在后台任务真正开始之前；补一个终态事件让 SSE 正常结束。
                if run.status == "canceled" and not context.terminal_emitted:
                    context.terminal_emitted = True
                    # 任务还没开始就被取消：补记助手消息计数，保证会话统计与消息行一致。
                    conversation = await db.get(Conversation, run.conversation_id)
                    if conversation is not None:
                        conversation.message_count += 1
                        conversation.last_message_at = _now()
                        conversation.last_message_preview = "（已取消）"
                        await db.commit()
                    await publisher.emit(
                        "message.done",
                        {
                            "message_id": (
                                str(run.assistant_message_id) if run.assistant_message_id else ""
                            ),
                            "usage": _usage_dict(None),
                            "finish_reason": "canceled",
                            "model": run.model,
                            "first_token_ms": None,
                            "duration_ms": 0,
                            "active_duration_ms": 0,
                            "citations": context.citations,
                            "artifacts": [],
                        },
                        event_type="message",
                        status="canceled",
                        title="已取消",
                    )
                return
            conversation = await db.get(Conversation, run.conversation_id)
            user_message = (
                await db.get(Message, run.user_message_id) if run.user_message_id else None
            )
            assistant_message = (
                await db.get(Message, run.assistant_message_id)
                if run.assistant_message_id
                else None
            )
            if conversation is None or user_message is None or assistant_message is None:
                _raise_missing_run_data()
            user = await db.get(User, conversation.user_id)
            if user is None:
                _raise_missing_run_data()
            started_at = run.started_at or started_wall
            conversation_id = conversation.id
            conversation_title = conversation.title
            user_message_id = user_message.id
            assistant_message_id = assistant_message.id
            user_message_created = user_message.created_at
            user_text = user_message.content
            client_message_id = user_message.client_message_id or ""
            requested_effort = run.reasoning_effort or "low"
            model_name = run.model
            run_capabilities = run.capabilities or {}

            # 能力装配：附件快照、指令、记忆、技能与 MCP 工具目录。
            assembled = await capabilities_module.assemble(
                db,
                user,
                attachment_ids=[uuid.UUID(item) for item in run_capabilities.get("attachments", [])],
                skill_ids=[uuid.UUID(item) for item in run_capabilities.get("skills", [])],
                mcp_server_ids=[uuid.UUID(item) for item in run_capabilities.get("mcp_servers", [])],
                mcp_tool_ids=[uuid.UUID(item) for item in run_capabilities.get("mcp_tools", [])],
            )
            if assembled.attachment_snapshot:
                user_message.attachments = assembled.attachment_snapshot
                await db.commit()

            runner = ToolRunner(
                run_id=run_id,
                publisher=publisher,
                user_id=user.id,
                user_role=user.role,
                mcp_tools=assembled.mcp_tool_map,
                active_skill_ids=assembled.active_skill_ids,
                a2a_agents=assembled.a2a_agent_map,
                conversation_id=conversation_id,
                # 与 RunContext 共享同一个 list：工具追加，终态读取。
                citations=context.citations,
                knowledge_categories=[
                    str(item) for item in run_capabilities.get("knowledge_categories", [])
                ],
            )
            tools = [
                *build_native_tools(runner),
                build_search_knowledge_tool(runner),
                *build_mcp_tools(runner, assembled.mcp_tool_map),
            ]
            send_message_tool = build_send_message_tool(runner, assembled.a2a_agent_map)
            if send_message_tool is not None:
                tools.append(send_message_tool)
            model_input = user_text
            if assembled.attachment_text:
                model_input = f"{user_text}\n\n{assembled.attachment_text}"

        if is_new_conversation:
            await publisher.emit(
                "conversation.created",
                {"conversation_id": str(conversation_id), "title": conversation_title},
                event_type="message",
                title="创建会话",
            )
        await publisher.emit(
            "run.started",
            {
                "conversation_id": str(conversation_id),
                "user_message_id": str(user_message_id),
                "client_message_id": client_message_id,
                "model": model_name,
                "reasoning_effort": requested_effort,
                "workflow_id": None,
                "workflow_version": None,
            },
            event_type="request",
            title="用户请求",
            subtitle=user_text[:80],
        )
        await publisher.emit(
            "message.created",
            {
                "message_id": str(user_message_id),
                "role": "user",
                "client_message_id": client_message_id,
                "created_at": to_iso(user_message_created),
            },
            event_type="message",
            title="用户消息",
            subtitle=user_text[:80],
        )
        await publisher.emit(
            "message.started",
            {
                "message_id": str(assistant_message_id),
                "role": "assistant",
                "started_at": to_iso(_now()),
            },
            event_type="message",
            status="running",
            title="助手消息",
        )
        for activation in assembled.skill_activations:
            await publisher.emit(
                "skill.activated",
                activation,
                event_type="tool",
                title=f"技能已激活：{activation['name']}",
                subtitle=activation["slug"],
            )
        for notice in assembled.notices:
            await publisher.emit(
                "notice",
                notice,
                event_type="notice",
                status="warning" if notice.get("level") == "warning" else "ok",
                title=notice.get("code") or "提示",
                subtitle=notice.get("message") or "",
            )

        model = build_chat_model(settings)
        async with open_checkpointer() as saver:
            graph = build_graph(
                model,
                saver,
                system_prompt=assembled.system_prompt,
                tools=tools,
                tool_executor=runner.execute,
                max_tool_rounds=settings.max_tool_rounds,
            )
            config = {"configurable": {"thread_id": str(conversation_id)}}
            async for chunk, metadata in graph.astream(
                {"messages": [HumanMessage(content=model_input)]},
                config,
                stream_mode="messages",
            ):
                if context.cancel_requested:
                    raise asyncio.CancelledError  # noqa: TRY301 - 取消信号需要冒泡到外层
                if metadata.get("langgraph_node") != "model":
                    continue
                # stream_mode="messages" 下 ainvoke 节点可能给出完整 AIMessage
                # 或 AIMessageChunk；两者都带 content/additional_kwargs/usage_metadata。
                if not hasattr(chunk, "content"):
                    continue

                reasoning = chunk.additional_kwargs.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    if reasoning_started is None:
                        reasoning_started = _now()
                    reasoning_parts.append(reasoning)
                    await publisher.emit(
                        "reasoning.delta",
                        {"message_id": str(assistant_message_id), "delta": reasoning},
                        persist=False,
                    )

                text = _content_text(chunk.content)
                if text:
                    if first_token_ms is None:
                        first_token_ms = elapsed_ms(started_at)
                    content_parts.append(text)
                    await publisher.emit(
                        "message.delta",
                        {"message_id": str(assistant_message_id), "delta": text},
                        persist=False,
                    )
                if chunk.usage_metadata:
                    usage = dict(chunk.usage_metadata)

            snapshot = await graph.aget_state(config)
            messages = snapshot.values.get("messages", [])
            if messages:
                final_message = messages[-1]
                final_text = _content_text(getattr(final_message, "content", ""))
                if final_text and not content_parts:
                    content_parts.append(final_text)
                if getattr(final_message, "usage_metadata", None):
                    usage = dict(final_message.usage_metadata)

        now = _now()
        duration_ms = elapsed_ms(started_at, now)
        notes = _usage_dict(usage)
        content = "".join(content_parts)

        if reasoning_parts:
            reasoning_text = "".join(reasoning_parts)
            await publisher.emit(
                "reasoning.done",
                {
                    "message_id": str(assistant_message_id),
                    "duration_ms": int(
                        ((_now() - (reasoning_started or started_at)).total_seconds()) * 1000
                    ),
                    "tokens": notes["reasoning_tokens"],
                    "summary": reasoning_text[:2000],
                },
                event_type="message",
                title="思考摘要",
            )

        completed = await _finalize_success(
            run_id=run_id,
            assistant_message_id=assistant_message_id,
            content=content,
            usage=notes,
            first_token_ms=first_token_ms,
            duration_ms=duration_ms,
            finished_at=now,
            citations=context.citations,
        )
        if not completed:
            await asyncio.shield(
                _finalize_canceled(
                    context=context,
                    content=content,
                    usage=notes,
                    first_token_ms=first_token_ms,
                    started_at=started_at or started_wall,
                )
            )
            return
        if context.terminal_emitted:
            return
        context.terminal_emitted = True
        await publisher.emit(
            "message.done",
            {
                "conversation_id": str(conversation_id),
                "message_id": str(assistant_message_id),
                "usage": notes,
                "finish_reason": "stop",
                "model": model_name,
                "first_token_ms": first_token_ms,
                "duration_ms": duration_ms,
                "active_duration_ms": duration_ms,
                "citations": context.citations,
                "artifacts": [],
                "reasoning": (
                    {
                        "duration_ms": int(
                            ((_now() - (reasoning_started or started_at)).total_seconds()) * 1000
                        ),
                        "tokens": notes["reasoning_tokens"],
                        "summary": "".join(reasoning_parts)[:2000],
                    }
                    if reasoning_parts
                    else None
                ),
            },
            event_type="message",
            title="助手回复",
            subtitle=content[:80],
            duration_ms=duration_ms,
            tokens=notes,
        )
    except asyncio.CancelledError:
        logger.info("运行被取消 run_id=%s", run_id)
        await asyncio.shield(
            _finalize_canceled(
                context=context,
                content="".join(content_parts),
                usage=_usage_dict(usage),
                first_token_ms=first_token_ms,
                started_at=started_at or started_wall,
            )
        )
    except ApprovalTimeoutError as exc:
        logger.info("审批超时 run_id=%s approval_id=%s", run_id, exc.approval_id)
        await asyncio.shield(
            _finalize_interrupted(
                context=context,
                content="".join(content_parts),
                usage=_usage_dict(usage),
                first_token_ms=first_token_ms,
                started_at=started_at or started_wall,
            )
        )
    except Exception:
        logger.exception("运行失败 run_id=%s", run_id)
        await asyncio.shield(
            _finalize_failed(
                context=context,
                content="".join(content_parts),
                usage=_usage_dict(usage),
                first_token_ms=first_token_ms,
                started_at=started_at or started_wall,
            )
        )


async def _finalize_success(
    *,
    run_id: uuid.UUID,
    assistant_message_id: uuid.UUID,
    content: str,
    usage: dict[str, int],
    first_token_ms: int | None,
    duration_ms: int,
    finished_at: datetime,
    citations: list[dict],
) -> bool:
    async with get_session_factory()() as db:
        assistant = await db.get(Message, assistant_message_id)
        run = await db.get(Run, run_id)
        if run is not None and run.status == "canceled":
            if assistant is not None:
                assistant.content = content
                assistant.status = "canceled"
                assistant.usage = usage
                assistant.citations = citations
            await db.commit()
            return False
        if assistant is not None:
            assistant.content = content
            assistant.status = "completed"
            assistant.usage = usage
            assistant.citations = citations
        if run is not None:
            run.status = "succeeded"
            run.input_tokens = usage["input_tokens"]
            run.output_tokens = usage["output_tokens"]
            run.reasoning_tokens = usage["reasoning_tokens"]
            run.first_token_ms = first_token_ms
            run.duration_ms = duration_ms
            run.active_duration_ms = duration_ms
            run.finished_at = finished_at
            conversation = await db.get(Conversation, run.conversation_id)
            if conversation is not None:
                conversation.last_message_at = finished_at
                conversation.last_message_preview = content[:80]
                conversation.message_count += 1
        await db.commit()
    return True


async def _finalize_canceled(
    *,
    context: RunContext,
    content: str,
    usage: dict[str, int],
    first_token_ms: int | None,
    started_at: datetime,
) -> None:
    if context.terminal_emitted:
        return
    context.terminal_emitted = True
    run_id = context.run_id
    publisher = context.publisher
    now = _now()
    duration_ms = elapsed_ms(started_at, now)
    async with get_session_factory()() as db:
        run = await db.get(Run, run_id)
        if run is None:
            return
        assistant = (
            await db.get(Message, run.assistant_message_id) if run.assistant_message_id else None
        )
        if assistant is not None:
            assistant.content = content
            assistant.status = "canceled"
            assistant.usage = usage
            assistant.citations = context.citations
        run.status = "canceled"
        run.canceled_at = now
        run.finished_at = now
        run.duration_ms = duration_ms
        run.active_duration_ms = duration_ms
        run.first_token_ms = first_token_ms
        run.input_tokens = usage["input_tokens"]
        run.output_tokens = usage["output_tokens"]
        run.reasoning_tokens = usage["reasoning_tokens"]
        conversation = await db.get(Conversation, run.conversation_id)
        if conversation is not None:
            conversation.last_message_at = now
            conversation.last_message_preview = content[:80] or "（已取消）"
            conversation.message_count += 1
        await db.commit()
        message_id = run.assistant_message_id
        model_name = run.model

    await publisher.emit(
        "message.done",
        {
            "message_id": str(message_id) if message_id else "",
            "usage": usage,
            "finish_reason": "canceled",
            "model": model_name,
            "first_token_ms": first_token_ms,
            "duration_ms": duration_ms,
            "active_duration_ms": duration_ms,
            "citations": context.citations,
            "artifacts": [],
        },
        event_type="message",
        status="canceled",
        title="已取消",
        duration_ms=duration_ms,
        tokens=usage,
    )


async def _finalize_interrupted(
    *,
    context: RunContext,
    content: str,
    usage: dict[str, int],
    first_token_ms: int | None,
    started_at: datetime,
) -> None:
    """审批超时：pending 审批置 expired，run 置 interrupted，流结束。"""
    if context.terminal_emitted:
        return
    context.terminal_emitted = True
    run_id = context.run_id
    publisher = context.publisher
    now = _now()
    duration_ms = elapsed_ms(started_at, now)
    async with get_session_factory()() as db:
        run = await db.get(Run, run_id)
        if run is None:
            return
        assistant = (
            await db.get(Message, run.assistant_message_id) if run.assistant_message_id else None
        )
        if assistant is not None:
            assistant.content = content
            assistant.status = "completed"
            assistant.usage = usage
            assistant.citations = context.citations
        run.status = "interrupted"
        run.duration_ms = duration_ms
        run.active_duration_ms = duration_ms
        run.first_token_ms = first_token_ms
        run.input_tokens = usage["input_tokens"]
        run.output_tokens = usage["output_tokens"]
        run.reasoning_tokens = usage["reasoning_tokens"]
        await db.execute(
            update(Approval)
            .where(Approval.run_id == run_id, Approval.status == "pending")
            .values(status="expired")
        )
        conversation = await db.get(Conversation, run.conversation_id)
        if conversation is not None:
            conversation.last_message_at = now
            conversation.last_message_preview = content[:80] or "（等待审批超时）"
            conversation.message_count += 1
        await db.commit()
        message_id = run.assistant_message_id
        model_name = run.model

    await publisher.emit(
        "message.done",
        {
            "message_id": str(message_id) if message_id else "",
            "usage": usage,
            "finish_reason": "interrupted",
            "model": model_name,
            "first_token_ms": first_token_ms,
            "duration_ms": duration_ms,
            "active_duration_ms": duration_ms,
            "citations": context.citations,
            "artifacts": [],
            "reason": "approval_timeout",
        },
        event_type="message",
        status="interrupted",
        title="等待审批超时",
        duration_ms=duration_ms,
        tokens=usage,
    )


async def _finalize_failed(
    *,
    context: RunContext,
    content: str,
    usage: dict[str, int],
    first_token_ms: int | None,
    started_at: datetime,
) -> None:
    if context.terminal_emitted:
        return
    context.terminal_emitted = True
    run_id = context.run_id
    publisher = context.publisher
    now = _now()
    duration_ms = elapsed_ms(started_at, now)
    message = "模型调用失败，请稍后重试"
    async with get_session_factory()() as db:
        run = await db.get(Run, run_id)
        if run is None:
            return
        assistant = (
            await db.get(Message, run.assistant_message_id) if run.assistant_message_id else None
        )
        if assistant is not None:
            assistant.content = content
            assistant.status = "failed"
            assistant.usage = usage
            assistant.citations = context.citations
            assistant.error = {"code": "llm_error", "message": message, "retryable": True}
        run.status = "failed"
        run.error_code = "llm_error"
        run.error_message = message
        run.finished_at = now
        run.duration_ms = duration_ms
        run.active_duration_ms = duration_ms
        run.first_token_ms = first_token_ms
        conversation = await db.get(Conversation, run.conversation_id)
        if conversation is not None:
            conversation.last_message_at = now
            conversation.last_message_preview = content[:80] or "（失败）"
            conversation.message_count += 1
        await db.commit()

    await publisher.emit(
        "error",
        {
            "code": "llm_error",
            "message": message,
            "request_id": current_request_id(),
            "run_id": str(run_id),
            "retryable": True,
        },
        event_type="error",
        status="error",
        title="运行失败",
        error={"code": "llm_error", "message": message},
    )


async def get_run(db: AsyncSession, user: User, run_id: uuid.UUID) -> RunOut:
    run = await _load_owned_run(db, user, run_id)
    now = _now()
    started_at = run.started_at or run.created_at
    duration_ms = elapsed_ms(started_at, run.finished_at or now)
    usage = _usage_out(
        {
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "reasoning_tokens": run.reasoning_tokens,
            "total_tokens": run.input_tokens + run.output_tokens,
        }
    )
    pending = await db.scalar(
        select(Approval)
        .where(
            Approval.run_id == run.id,
            Approval.status == "pending",
            or_(Approval.expires_at.is_(None), Approval.expires_at > now),
        )
        .order_by(Approval.created_at.desc())
        .limit(1)
    )
    pending_out = RunPendingOut()
    if pending is not None:
        pending_out = RunPendingOut(kind="approval", approval_id=pending.id)

    # A2A 等待：run 内存在 input_required / auth_required 任务时暴露 pending。
    a2a_rows = list(
        (
            await db.execute(
                select(A2aTask).where(A2aTask.run_id == run.id).order_by(A2aTask.created_at.asc())
            )
        ).scalars()
    )
    waiting = [
        item for item in a2a_rows if item.state in {"input_required", "auth_required"}
    ]
    if waiting and pending_out.kind is None:
        latest = waiting[-1]
        pending_out = RunPendingOut(
            kind="input_required" if latest.state == "input_required" else "auth_required",
            task_id=latest.id,
        )
    agents: list[RunAgentOut] = [
        RunAgentOut(
            kind="local",
            id="main-agent",
            name="main-agent",
            model=run.model or None,
            state="online" if run.status == "running" else "idle",
        )
    ] if run.model else []
    if a2a_rows:
        agent_ids = {item.remote_agent_id for item in a2a_rows}
        remote_rows = (
            await db.execute(
                select(RemoteAgent).where(RemoteAgent.id.in_(agent_ids))
            )
        ).scalars()
        by_state: dict[uuid.UUID, str] = {}
        for item in a2a_rows:
            by_state[item.remote_agent_id] = item.state
        for remote in remote_rows:
            agents.append(
                RunAgentOut(
                    kind="a2a",
                    id=str(remote.id),
                    slug=remote.slug,
                    name=remote.name,
                    state=by_state.get(remote.id, "idle"),
                )
            )
    error = (
        MessageErrorOut(code=run.error_code, message=run.error_message, retryable=True)
        if run.error_code
        else None
    )
    return RunOut(
        id=run.id,
        conversation_id=run.conversation_id,
        status=run.status,  # type: ignore[arg-type]
        source=run.source,
        model=run.model or None,
        reasoning_effort=run.reasoning_effort,
        workflow=None,
        capabilities=_normalize_capabilities(run.capabilities),
        usage=usage,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=run.duration_ms or duration_ms,
        active_duration_ms=run.active_duration_ms or duration_ms,
        first_token_ms=run.first_token_ms,
        pending=pending_out,
        message_ids=RunMessageIdsOut(user=run.user_message_id, assistant=run.assistant_message_id),
        agents=agents,
        error=error,
    )


async def list_run_events(
    db: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    *,
    limit: int,
    after_seq: int | None,
    types: str | None,
    q: str | None,
    cursor: str | None,
) -> RunEventPage:
    await _load_owned_run(db, user, run_id)
    stmt = select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq.asc())
    if after_seq is not None:
        stmt = stmt.where(RunEvent.seq > after_seq)
    if cursor:
        from app.core.cursor import decode_cursor

        raw = decode_cursor(cursor).get("seq")
        if isinstance(raw, int):
            stmt = stmt.where(RunEvent.seq > raw)
    if types:
        allowed = [item.strip() for item in types.split(",") if item.strip()]
        if allowed:
            stmt = stmt.where(RunEvent.type.in_(allowed))
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(or_(RunEvent.title.ilike(pattern), RunEvent.subtitle.ilike(pattern)))
    stmt = stmt.limit(limit + 1)
    rows = list((await db.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    items: list[RunEventOut] = []
    for row in rows:
        payload = {k: v for k, v in (row.payload or {}).items() if k != "_event"}
        error_message = None
        if isinstance(row.error, dict):
            error_message = str(row.error.get("message") or "")
        items.append(
            RunEventOut(
                id=f"{run_id}:{row.seq}",
                seq=row.seq,
                ts=row.ts,
                type=row.type,
                status=row.status,
                title=row.title,
                subtitle=row.subtitle,
                duration_ms=row.duration_ms,
                tokens=row.tokens or None,
                payload=payload,
                error=error_message,
            )
        )
    next_cursor = None
    if has_more and rows:
        next_cursor = encode_cursor({"seq": rows[-1].seq})
    return RunEventPage(items=items, next_cursor=next_cursor, has_more=has_more)


async def cancel_run(db: AsyncSession, user: User, run_id: uuid.UUID) -> RunCancelResult:
    run = await _load_owned_run(db, user, run_id)
    if run.status not in _ACTIVE_STATUSES:
        raise RunNotCancelableError(run.status)
    now = _now()
    started_at = run.started_at or run.created_at
    duration_ms = elapsed_ms(started_at, now)
    run.status = "canceled"
    run.canceled_at = now
    run.finished_at = now
    run.duration_ms = duration_ms
    run.active_duration_ms = duration_ms
    await db.execute(
        update(Message)
        .where(Message.run_id == run_id, Message.status == "streaming")
        .values(status="canceled")
    )
    await db.execute(
        update(Approval)
        .where(Approval.run_id == run_id, Approval.status == "pending")
        .values(status="canceled")
    )
    await db.commit()
    registry.cancel(run_id)
    return RunCancelResult(id=run.id, status="canceled", canceled_at=now)


async def resume_run(
    db: AsyncSession, user: User, run_id: uuid.UUID, user_input: str
) -> RunResumeResult:
    del user_input
    run = await _load_owned_run(db, user, run_id)
    if run.status != "interrupted":
        raise ConflictError("当前运行不处于等待输入状态", code="run_not_resumable")
    # M1 还没有会触发 interrupt 的工具；保留端点形状，接入 A2A/MCP 后实现 Command(resume)。
    raise ConflictError("当前版本不支持恢复运行", code="run_not_resumable")


async def stream_run_events(run_id: uuid.UUID, *, after_seq: int = 0) -> AsyncIterator[str]:
    """SSE 生成器：先回放已落库事件，再跟随实时事件。"""
    settings = get_settings()
    queue = await bus.subscribe(run_id)
    last_seq = after_seq
    try:
        async with get_session_factory()() as db:
            rows = list(
                (
                    await db.execute(
                        select(RunEvent)
                        .where(RunEvent.run_id == run_id, RunEvent.seq > last_seq)
                        .order_by(RunEvent.seq.asc())
                    )
                )
                .scalars()
                .all()
            )
        for row in rows:
            if row.seq <= last_seq:
                continue
            event_name = str((row.payload or {}).get("_event") or "")
            if not event_name:
                continue
            data = {k: v for k, v in (row.payload or {}).items() if k != "_event"}
            last_seq = row.seq
            yield format_sse(row.seq, event_name, data)
            if event_name in TERMINAL_EVENTS:
                return

        idle_seconds = 0
        heartbeat_seconds = 15
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
            except TimeoutError:
                idle_seconds += heartbeat_seconds
                if idle_seconds >= settings.hitl_stream_timeout_seconds:
                    logger.info("SSE 等待超时 run_id=%s", run_id)
                    return
                yield ": ping\n\n"
                continue
            idle_seconds = 0
            data = item.get("data", {})
            seq = int(data.get("seq") or 0)
            if seq <= last_seq:
                continue
            last_seq = seq
            event_name = str(item.get("event") or "")
            yield format_sse(seq, event_name, data)
            if event_name in TERMINAL_EVENTS:
                return
    finally:
        await bus.unsubscribe(run_id, queue)
