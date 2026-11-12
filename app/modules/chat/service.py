"""聊天与运行业务：创建 run、执行图、事件流、取消与查询。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.types import Command
from opentelemetry import trace
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.callbacks import ObservabilityCallbackHandler
from app.agents.checkpointer import open_checkpointer
from app.agents.graph import build_graph
from app.agents.models import build_chat_model
from app.core.audit import content_fingerprint, write_audit
from app.core.config import get_settings
from app.core.cursor import encode_cursor
from app.core.errors import (
    BadRequestError,
    CapabilityDisabledError,
    ConflictError,
    NotFoundError,
    RunInProgressError,
    RunNotCancelableError,
    ValidationFailedError,
    current_request_id,
)
from app.core.log_context import bind_run_context, run_id_ctx
from app.core.metrics import record_approval, record_chat_run
from app.core.prompt_guard import SUSPICIOUS_INPUT_NOTE, sanitize_output, scan_injection
from app.core.rate_limit import limiter
from app.core.stream_filter import StreamingOutputFilter
from app.core.telemetry import get_tracer, start_span
from app.core.timeutil import elapsed_ms
from app.core.urls import validate_authorization_url
from app.db.models.approval import Approval
from app.db.models.chat import Run, RunEvent
from app.db.models.conversation import Conversation, Message
from app.db.models.remote import A2aTask, RemoteAgent
from app.db.models.user import User
from app.db.models.workflow import Workflow, WorkflowRun
from app.db.session import get_session_factory
from app.modules.chat import capabilities as capabilities_module
from app.modules.chat.approvals import build_approval_gate, build_halt_gate
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
from app.modules.workflows.assembler import (
    filter_tools,
    force_approval,
    prompt_section,
    tool_allowlist,
    validate_steps,
)
from app.modules.workflows.service import get_visible_workflow, sync_workflow_run_status

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = ("queued", "running", "interrupted")
# SSE 续流端点单次回放上限；旧 POST /chat/stream 不传 max_replay，保持不截断。
SSE_MAX_REPLAY = 1000
# run 已终态后不会再产生终态事件，续流端点回放完可直接关闭。
TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "canceled"})
# SSE 空闲心跳周期：等待实时事件超过该周期就发一条注释帧（不占 seq）。
_SSE_HEARTBEAT_SECONDS = 15
_DEFAULT_CAPABILITIES: dict[str, Any] = {
    "skills": [],
    "mcp_servers": [],
    "mcp_tools": [],
    "knowledge_categories": [],
    "workflow_id": None,
}


_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn_background(coro) -> None:
    """保持后台清理任务的引用，避免被 GC 回收（RUF006）。"""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


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
    workflow_id 的校验已交给 ``_validate_workflow``（M9），此处不再占位拒绝。
    """
    if payload.knowledge_categories and not get_settings().knowledge_enabled:
        raise CapabilityDisabledError("知识库能力未启用")


async def _validate_workflow(
    db: AsyncSession, user: User, payload: ChatStreamRequest
) -> Workflow | None:
    """按模板语义校验请求携带的 workflow_id，返回可见模板行；未携带返回 None。

    错误语义（spike 报告 §3.1）：不存在/不可见 → 404（不泄露他人模板存在性）；
    已停用 → 409 ``workflow_disabled``；请求版本与当前 version 不匹配 → 409
    ``workflow_version_conflict``；steps 未过装配器校验 → 422
    ``workflow_steps_invalid``（消息拼前 3 条）。``workflows_enabled`` 开关门禁
    属于 T7，本任务不接入。
    """
    if payload.workflow_id is None:
        return None
    workflow = await get_visible_workflow(db, user, payload.workflow_id)
    if workflow is None:
        raise NotFoundError("工作流不存在")
    if not workflow.enabled:
        raise ConflictError("工作流已停用，无法在此轮使用", code="workflow_disabled")
    if payload.workflow_version is not None and payload.workflow_version != workflow.version:
        raise ConflictError(
            "工作流版本已更新，请刷新模板后重试", code="workflow_version_conflict"
        )
    errors = validate_steps(workflow.steps)
    if errors:
        raise ValidationFailedError(
            f"工作流步骤不合法：{'；'.join(errors[:3])}", code="workflow_steps_invalid"
        )
    return workflow


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
    # 聊天限流：按用户滑动窗口，与登录/注册限流共用进程内实现；多实例需共享限流（后续）。
    limiter.check(
        f"chat:{user.id}",
        settings.rate_limit_chat_max,
        settings.rate_limit_chat_window_seconds,
        scope="chat",
    )
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
    workflow = await _validate_workflow(db, user, payload)
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
    if workflow is not None:
        # M9 模板快照（与 run 同一事务原子落库）：steps/name 冻结进 capabilities，
        # 恢复轮与模板 PATCH/删除解耦；workflow_name 供 T7 的 run 查询在模板
        # 删除后仍可显示。
        capabilities["workflow_version"] = workflow.version
        capabilities["workflow_steps"] = list(workflow.steps or [])
        capabilities["workflow_name"] = workflow.name

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
        # M9：模板列快照（无外键，模板删除不影响 run 读取）。
        workflow_id=payload.workflow_id,
        workflow_version=workflow.version if workflow is not None else None,
    )
    db.add(run)
    await db.flush()
    user_message.run_id = run.id
    assistant_message.run_id = run.id
    if workflow is not None:
        # M9：runs 行先 flush（FK 依赖 run.id），再插 workflow_runs 映射行；
        # 创建即 running，终态由 _finalize_* 里的 sync_workflow_run_status 追平。
        db.add(WorkflowRun(workflow_id=workflow.id, run_id=run.id, status="running"))

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


@dataclass
class RunRuntime:
    """一次运行所需的装配结果；首轮与恢复共用，避免两套逻辑漂移。"""

    run_id: uuid.UUID
    conversation_id: uuid.UUID
    conversation_title: str
    user_id: uuid.UUID
    run: Run
    runner: ToolRunner
    tools: list[Any]
    model: Any
    system_prompt: str
    model_name: str
    model_input: str
    started_at: datetime
    started_wall: datetime
    first_token_ms: int | None
    content_parts: list[str]
    reasoning_parts: list[str]
    usage: dict[str, Any]
    user_message_id: uuid.UUID
    user_text: str
    client_message_id: str
    user_message_created: datetime
    reasoning_effort: str
    notices: list[dict[str, Any]]
    skill_activations: list[dict[str, Any]]


def _add_usage(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key in ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"):
        if key in extra:
            result[key] = int(result.get(key) or 0) + int(extra.get(key) or 0)
    return result


async def _write_security_audit(
    *,
    actor_id: uuid.UUID,
    action: str,
    resource_id: str,
    detail: dict[str, Any],
) -> None:
    """安全审计独立短事务，避免影响主流程。"""
    async with get_session_factory()() as db:
        await write_audit(
            db,
            actor_type="user",
            actor_id=str(actor_id),
            action=action,
            resource_type="run" if action.startswith("security.output") else "message",
            resource_id=resource_id,
            detail=detail,
            commit=True,
        )


async def _redact_before_store(run_id: uuid.UUID, content: str) -> str:
    """落库前统一打码；命中时写安全审计。"""
    if not get_settings().prompt_guard_redact_output or not content:
        return content
    redacted, rules = sanitize_output(content)
    if not rules:
        return content
    async with get_session_factory()() as db:
        run = await db.get(Run, run_id)
        conversation = (
            await db.get(Conversation, run.conversation_id) if run is not None else None
        )
    if conversation is not None:
        await _write_security_audit(
            actor_id=conversation.user_id,
            action="security.output_redacted",
            resource_id=str(run_id),
            detail={"rules": rules, "fingerprint": content_fingerprint(content)},
        )
    return redacted


async def _wait_run_task(run_id: uuid.UUID, timeout: float = 10.0) -> None:
    """等待同一 run 的旧任务收尾（避免跨任务事件 seq 冲突）。"""
    existing = registry.get(run_id)
    task = existing.task if existing is not None else None
    if task is not None and not task.done():
        await asyncio.wait({task}, timeout=timeout)


async def _event_start_seq(run_id: uuid.UUID) -> int:
    async with get_session_factory()() as db:
        value = await db.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id))
    return int(value or 0)


async def prepare_runtime(
    db,
    run: Run,
    publisher: RunEventPublisher,
    context: RunContext,
    *,
    resumed: bool,
) -> RunRuntime:
    """能力装配 + 工具目录 + 模型；恢复时从 DB 读回已产出内容/引用/usage。"""
    conversation = await db.get(Conversation, run.conversation_id)
    user_message = await db.get(Message, run.user_message_id) if run.user_message_id else None
    assistant_message = (
        await db.get(Message, run.assistant_message_id) if run.assistant_message_id else None
    )
    if conversation is None or user_message is None or assistant_message is None:
        _raise_missing_run_data()
    user = await db.get(User, conversation.user_id)
    if user is None:
        _raise_missing_run_data()
    run_capabilities = run.capabilities or {}

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

    # M9：从 capabilities 快照读模板步骤与名称。恢复轮模板可能已被 PATCH/删除，
    # 必须用快照（快照原则：一次 run 的执行语义在 start_chat 时冻结）；快照缺失
    # （异常数据）回退查表兜底，查不到或 steps 已不合法则按运行关联数据缺失处理。
    workflow_steps: list[dict[str, Any]] = []
    workflow_name = ""
    raw_workflow_id = run_capabilities.get("workflow_id")
    if raw_workflow_id:
        snapshot_steps = run_capabilities.get("workflow_steps")
        if isinstance(snapshot_steps, list) and snapshot_steps:
            workflow_steps = [dict(item) for item in snapshot_steps if isinstance(item, dict)]
            workflow_name = str(run_capabilities.get("workflow_name") or "")
        else:
            try:
                workflow = await db.get(Workflow, uuid.UUID(str(raw_workflow_id)))
            except (ValueError, TypeError):
                workflow = None
            if workflow is None or validate_steps(workflow.steps):
                _raise_missing_run_data()
            workflow_steps = list(workflow.steps or [])
            workflow_name = workflow.name

    runner = ToolRunner(
        run_id=run.id,
        publisher=publisher,
        user_id=user.id,
        user_role=user.role,
        mcp_tools=assembled.mcp_tool_map,
        active_skill_ids=assembled.active_skill_ids,
        a2a_agents=assembled.a2a_agent_map,
        conversation_id=conversation.id,
        # 与 RunContext 共享同一个 list：工具追加，终态读取。
        citations=context.citations,
        knowledge_categories=[
            str(item) for item in run_capabilities.get("knowledge_categories", [])
        ],
        # M9：步骤快照与强制审批集合（消费逻辑属 T6）。
        workflow_steps=workflow_steps,
        force_approval_tools=force_approval(workflow_steps),
    )
    tools = [
        *build_native_tools(runner),
        build_search_knowledge_tool(runner),
        *build_mcp_tools(runner, assembled.mcp_tool_map),
    ]
    send_message_tool = build_send_message_tool(runner, assembled.a2a_agent_map)
    if send_message_tool is not None:
        tools.append(send_message_tool)
    # M9：按模板白名单过滤工具（空白名单不过滤，防御坏模板）；保持原顺序。
    tools = filter_tools(tools, tool_allowlist(workflow_steps))

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: dict[str, Any] = {}
    if resumed:
        if assistant_message.content:
            content_parts.append(assistant_message.content)
        if assistant_message.citations:
            context.citations.extend(assistant_message.citations)
        usage = {
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "reasoning_tokens": run.reasoning_tokens,
            "total_tokens": run.input_tokens + run.output_tokens,
        }

    system_prompt = assembled.system_prompt
    # M9：模板段落拼在 prompt_guard 附注之前，保证安全提醒始终位于提示词末尾。
    if workflow_steps:
        system_prompt = f"{system_prompt}\n\n{prompt_section(workflow_name, workflow_steps)}"
    settings = get_settings()
    if settings.prompt_guard_enabled:
        findings = scan_injection(user_message.content)
        if findings:
            system_prompt = f"{system_prompt}\n\n{SUSPICIOUS_INPUT_NOTE}"
            if not resumed:
                await _write_security_audit(
                    actor_id=user.id,
                    action="security.injection_suspected",
                    resource_id=str(user_message.id),
                    detail={
                        "rules": sorted({item.rule for item in findings}),
                        "fingerprint": content_fingerprint(user_message.content),
                        "message_length": len(user_message.content),
                    },
                )
    model_input = user_message.content
    if assembled.attachment_text:
        model_input = f"{user_message.content}\n\n{assembled.attachment_text}"

    return RunRuntime(
        run_id=run.id,
        conversation_id=conversation.id,
        conversation_title=conversation.title,
        user_id=user.id,
        run=run,
        runner=runner,
        tools=tools,
        model=build_chat_model(get_settings()),
        system_prompt=system_prompt,
        model_name=run.model,
        model_input=model_input,
        started_at=run.started_at or _now(),
        started_wall=_now(),
        first_token_ms=run.first_token_ms if resumed else None,
        content_parts=content_parts,
        reasoning_parts=reasoning_parts,
        usage=usage,
        user_message_id=user_message.id,
        user_text=user_message.content,
        client_message_id=user_message.client_message_id or "",
        user_message_created=user_message.created_at,
        reasoning_effort=run.reasoning_effort or "low",
        notices=list(assembled.notices),
        skill_activations=list(assembled.skill_activations),
    )


def _build_graph_for(runtime: RunRuntime, saver, *, halt: bool = False):
    gate = build_halt_gate() if halt else build_approval_gate(runtime.runner)
    return build_graph(
        runtime.model,
        saver,
        system_prompt=runtime.system_prompt,
        tools=runtime.tools,
        tool_executor=runtime.runner.execute,
        approval_gate=gate,
        max_tool_rounds=get_settings().max_tool_rounds,
    )


async def _clear_stale_interrupt(saver, runtime: RunRuntime, config: dict[str, Any]) -> None:
    """新 run 开始前清理同一会话线程里残留的 interrupt（防御性兜底）。"""
    probe = _build_graph_for(runtime, saver, halt=True)
    snapshot = await probe.aget_state(config)
    if not snapshot.next:
        return
    logger.warning(
        "清理残留 interrupt run_id=%s thread=%s", runtime.run_id, config["configurable"]["thread_id"]
    )
    async for _ in probe.astream(
        Command(resume={"decision": "cancel"}), config, stream_mode="messages"
    ):
        pass


async def _stream_graph(
    context: RunContext,
    runtime: RunRuntime,
    graph,
    config: dict[str, Any],
    graph_input: Any,
    *,
    is_new_conversation: bool = False,
    resumed: bool = False,
) -> None:
    """chat.run span 包装；子 span（LLM/工具）通过当前上下文自动挂到它下面。"""
    with start_span(
        "chat.run",
        attributes={
            "app.run_id": str(runtime.run_id),
            "app.conversation_id": str(runtime.conversation_id),
            "gen_ai.request.model": runtime.model_name,
            "app.resumed": resumed,
        },
    ):
        await _stream_graph_inner(
            context,
            runtime,
            graph,
            config,
            graph_input,
            is_new_conversation=is_new_conversation,
            resumed=resumed,
        )


async def _stream_graph_inner(
    context: RunContext,
    runtime: RunRuntime,
    graph,
    config: dict[str, Any],
    graph_input: Any,
    *,
    is_new_conversation: bool = False,
    resumed: bool = False,
) -> None:
    """执行图并持续发事件；挂起时落库部分内容并发 run.interrupted。"""
    publisher = context.publisher
    run_id = runtime.run_id
    started_at = runtime.started_at
    # LLM 回调在模型节点内执行；span/指标由回调统一记录，避免请求层重复计时。
    config.setdefault("callbacks", []).append(ObservabilityCallbackHandler(runtime.model_name))
    first_token_ms: int | None = runtime.first_token_ms
    content_parts = runtime.content_parts
    reasoning_parts = runtime.reasoning_parts
    reasoning_started: datetime | None = None
    usage = runtime.usage

    try:
        if resumed:
            await publisher.emit(
                "run.resumed",
                {
                    "run_id": str(run_id),
                    "conversation_id": str(runtime.conversation_id),
                    "status": "running",
                },
                event_type="run",
                title="继续运行",
            )
        else:
            if is_new_conversation:
                await publisher.emit(
                    "conversation.created",
                    {
                        "conversation_id": str(runtime.conversation_id),
                        "title": runtime.conversation_title,
                    },
                    event_type="message",
                    title="创建会话",
                )
            await publisher.emit(
                "run.started",
                {
                    "conversation_id": str(runtime.conversation_id),
                    "user_message_id": str(runtime.user_message_id),
                    "client_message_id": runtime.client_message_id,
                    "model": runtime.model_name,
                    "reasoning_effort": runtime.reasoning_effort,
                    "workflow_id": None,
                    "workflow_version": None,
                },
                event_type="request",
                title="用户请求",
                subtitle=runtime.user_text[:80],
            )
            await publisher.emit(
                "message.created",
                {
                    "message_id": str(runtime.user_message_id),
                    "role": "user",
                    "client_message_id": runtime.client_message_id,
                    "created_at": to_iso(runtime.user_message_created),
                },
                event_type="message",
                title="用户消息",
                subtitle=runtime.user_text[:80],
            )
            assistant_message_id = runtime.run.assistant_message_id
            await publisher.emit(
                "message.started",
                {
                    "message_id": str(assistant_message_id) if assistant_message_id else "",
                    "role": "assistant",
                    "started_at": to_iso(_now()),
                },
                event_type="message",
                status="running",
                title="助手消息",
            )
            for activation in runtime.skill_activations:
                await publisher.emit(
                    "skill.activated",
                    activation,
                    event_type="tool",
                    title=f"技能已激活：{activation['name']}",
                    subtitle=activation["slug"],
                )
            for notice in runtime.notices:
                await publisher.emit(
                    "notice",
                    notice,
                    event_type="notice",
                    status="warning" if notice.get("level") == "warning" else "ok",
                    title=notice.get("code") or "提示",
                    subtitle=notice.get("message") or "",
                )

        filters_enabled = get_settings().prompt_guard_redact_output
        text_filter = StreamingOutputFilter(enabled=filters_enabled)
        reasoning_filter = StreamingOutputFilter(enabled=filters_enabled)

        async for chunk, metadata in graph.astream(
            graph_input,
            config,
            stream_mode="messages",
        ):
            if context.cancel_requested:
                raise asyncio.CancelledError  # noqa: TRY301 - 取消信号需要冒泡到外层
            if metadata.get("langgraph_node") != "model":
                continue
            if not hasattr(chunk, "content"):
                continue

            reasoning = chunk.additional_kwargs.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                if reasoning_started is None:
                    reasoning_started = _now()
                filtered_reasoning = reasoning_filter.feed(reasoning)
                if filtered_reasoning:
                    reasoning_parts.append(filtered_reasoning)
                    await publisher.emit(
                        "reasoning.delta",
                        {
                            "message_id": str(runtime.run.assistant_message_id),
                            "delta": filtered_reasoning,
                        },
                        persist=False,
                    )

            text = _content_text(chunk.content)
            if text:
                if first_token_ms is None:
                    first_token_ms = elapsed_ms(started_at)
                    from opentelemetry import trace

                    trace.get_current_span().set_attribute("app.first_token_ms", first_token_ms)
                filtered_text = text_filter.feed(text)
                if filtered_text:
                    content_parts.append(filtered_text)
                    await publisher.emit(
                        "message.delta",
                        {
                            "message_id": str(runtime.run.assistant_message_id),
                            "delta": filtered_text,
                        },
                        persist=False,
                    )
            if chunk.usage_metadata:
                usage = (
                    _add_usage(usage, dict(chunk.usage_metadata))
                    if resumed
                    else dict(chunk.usage_metadata)
                )

        tail = text_filter.flush()
        if tail:
            content_parts.append(tail)
            await publisher.emit(
                "message.delta",
                {
                    "message_id": str(runtime.run.assistant_message_id),
                    "delta": tail,
                },
                persist=False,
            )
        reasoning_tail = reasoning_filter.flush()
        if reasoning_tail:
            reasoning_parts.append(reasoning_tail)
            await publisher.emit(
                "reasoning.delta",
                {
                    "message_id": str(runtime.run.assistant_message_id),
                    "delta": reasoning_tail,
                },
                persist=False,
            )
        stream_rules = text_filter.rules | reasoning_filter.rules
        if stream_rules:
            await _write_security_audit(
                actor_id=runtime.user_id,
                action="security.output_redacted",
                resource_id=str(run_id),
                detail={"phase": "stream", "rules": sorted(stream_rules)},
            )

        snapshot = await graph.aget_state(config)
        if snapshot.next:
            await _finalize_waiting_approval(context=context, runtime=runtime)
            return
        messages = snapshot.values.get("messages", [])
        if messages:
            final_message = messages[-1]
            final_text = _content_text(getattr(final_message, "content", ""))
            if final_text and not content_parts:
                content_parts.append(final_text)
            if getattr(final_message, "usage_metadata", None) and not usage:
                usage = dict(final_message.usage_metadata)

        now = _now()
        duration_ms = elapsed_ms(started_at, now)
        notes = _usage_dict(usage)
        content = "".join(content_parts)
        if get_settings().prompt_guard_redact_output:
            redacted, redaction_rules = sanitize_output(content)
            if redaction_rules:
                await _write_security_audit(
                    actor_id=runtime.user_id,
                    action="security.output_redacted",
                    resource_id=str(run_id),
                    detail={
                        "rules": redaction_rules,
                        "fingerprint": content_fingerprint(content),
                    },
                )
                content = redacted
                content_parts[:] = [content]
        assistant_message_id = runtime.run.assistant_message_id

        if snapshot.values.get("halted"):
            await asyncio.shield(
                _finalize_canceled(
                    context=context,
                    content=content,
                    usage=notes,
                    first_token_ms=first_token_ms,
                    started_at=started_at,
                    reason="approval_timeout",
                )
            )
            return

        if reasoning_parts:
            await publisher.emit(
                "reasoning.done",
                {
                    "message_id": str(assistant_message_id) if assistant_message_id else "",
                    "duration_ms": elapsed_ms(reasoning_started or started_at),
                    "tokens": notes["reasoning_tokens"],
                    "summary": "".join(reasoning_parts)[:2000],
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
                    started_at=started_at,
                )
            )
            return
        if context.terminal_emitted:
            return
        context.terminal_emitted = True
        await publisher.emit(
            "message.done",
            {
                "conversation_id": str(runtime.conversation_id),
                "message_id": str(assistant_message_id) if assistant_message_id else "",
                "usage": notes,
                "finish_reason": "stop",
                "model": runtime.model_name,
                "first_token_ms": first_token_ms,
                "duration_ms": duration_ms,
                "active_duration_ms": duration_ms,
                "citations": context.citations,
                "artifacts": [],
                "reasoning": (
                    {
                        "duration_ms": elapsed_ms(reasoning_started or started_at),
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
                started_at=started_at,
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
                started_at=started_at,
            )
        )


async def execute_run(context: RunContext, is_new_conversation: bool) -> None:
    """首轮执行：装配运行时、构建图并驱动事件流。"""
    run_id = context.run_id
    try:
        async with get_session_factory()() as db:
            run = await db.get(Run, run_id)
            if run is None:
                return
            if run.status != "running":
                if run.status == "canceled" and not context.terminal_emitted:
                    context.terminal_emitted = True
                    conversation = await db.get(Conversation, run.conversation_id)
                    if conversation is not None:
                        conversation.message_count += 1
                        conversation.last_message_at = _now()
                        conversation.last_message_preview = "（已取消）"
                        # M9/T4 交接：该分支不走 _finalize_canceled，须在同一事务
                        # 内补同步 workflow_runs 行，避免 start 后立即取消的
                        # run 让映射行停留 running。
                        await sync_workflow_run_status(db, run.id, "canceled")
                        await db.commit()
                    await context.publisher.emit(
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
            runtime = await prepare_runtime(db, run, context.publisher, context, resumed=False)

        async with open_checkpointer() as saver:
            graph = _build_graph_for(runtime, saver)
            config = {"configurable": {"thread_id": str(runtime.conversation_id)}}
            await _clear_stale_interrupt(saver, runtime, config)
            with bind_run_context(runtime.run_id, runtime.conversation_id):
                await _stream_graph(
                    context,
                    runtime,
                    graph,
                    config,
                    {"messages": [HumanMessage(content=runtime.model_input)]},
                    is_new_conversation=is_new_conversation,
                )
    except asyncio.CancelledError:
        logger.info("运行被取消 run_id=%s", run_id)
        await asyncio.shield(
            _finalize_canceled(
                context=context,
                content="",
                usage=_usage_dict(None),
                first_token_ms=None,
                started_at=_now(),
            )
        )
    except Exception:
        logger.exception("运行失败 run_id=%s", run_id)
        await asyncio.shield(
            _finalize_failed(
                context=context,
                content="",
                usage=_usage_dict(None),
                first_token_ms=None,
                started_at=_now(),
            )
        )


async def schedule_resume(run_id: uuid.UUID, *, recovery: bool = False) -> bool:
    """调度审批恢复任务；返回是否成功调度（不等待图执行）。

    决定可能先于图挂起到达：等待旧 execute_run 任务收尾后再检查 checkpoint，
    避免「决定已落库但没有任务负责恢复」的竞态。
    """
    existing = registry.get(run_id)
    wait_task = (
        existing.task
        if existing is not None and existing.task is not None and not existing.task.done()
        else None
    )
    publisher = RunEventPublisher(run_id, get_session_factory(), bus)
    context = RunContext(run_id=run_id, publisher=publisher, recovery=recovery)
    registry.start(context, _resume_after_wait(context, wait_task))
    return True


async def _resume_after_wait(
    context: RunContext, wait_task: asyncio.Task | None
) -> None:
    """等待首轮任务结束（gate 挂起后 execute_run 返回），再走恢复。"""
    if wait_task is not None:
        await asyncio.wait({wait_task}, timeout=10)
    # 旧任务已发完 run.interrupted；此时再取 max(seq)，避免 seq 冲突/倒序。
    context.publisher.seq = await _event_start_seq(context.run_id)
    await resume_after_decision(context)


async def resume_after_decision(context: RunContext) -> None:
    """审批决定后的后台恢复：抢占 interrupted→running 并续跑同一条 run。"""
    run_id = context.run_id
    try:
        async with get_session_factory()() as db:
            updated = await db.execute(
                update(Run)
                .where(Run.id == run_id, Run.status == "interrupted")
                .values(status="running")
            )
            await db.commit()
            if not updated.rowcount:
                return
            run = await db.get(Run, run_id)
            if run is None:
                return
            runtime = await prepare_runtime(db, run, context.publisher, context, resumed=True)

        async with open_checkpointer() as saver:
            graph = _build_graph_for(runtime, saver)
            config = {"configurable": {"thread_id": str(runtime.conversation_id)}}
            snapshot = await graph.aget_state(config)
            if not snapshot.next:
                if context.recovery:
                    # 启动/周期恢复：没有挂起说明重启丢了 checkpoint 状态，
                    # 不能再把 run 丢回 interrupted 永久占锁。
                    logger.warning("恢复时 checkpoint 无挂起，按 restart_lost 取消 run_id=%s", run_id)
                    async with get_session_factory()() as db:
                        await db.execute(
                            update(Run)
                            .where(Run.id == run_id, Run.status == "running")
                            .values(status="interrupted")
                        )
                        await db.commit()
                    # 不能走 cancel_interrupted_run：它会等待当前 resume 任务结束，
                    # 在任务内部调用会自锁。这里直接写终态。
                    await _finalize_canceled(
                        context=context,
                        content="".join(runtime.content_parts),
                        usage=_usage_dict(runtime.usage),
                        first_token_ms=runtime.first_token_ms,
                        started_at=runtime.started_at,
                        reason="restart_lost",
                    )
                    return
                # 决定先于挂起：gate 已按 DB 状态继续，回滚抢占即可。
                logger.warning("恢复时 checkpoint 无挂起，回滚 running→interrupted run_id=%s", run_id)
                async with get_session_factory()() as db:
                    await db.execute(
                        update(Run)
                        .where(Run.id == run_id, Run.status == "running")
                        .values(status="interrupted")
                    )
                    await db.commit()
                return
            with bind_run_context(runtime.run_id, runtime.conversation_id):
                await _stream_graph(
                    context,
                    runtime,
                    graph,
                    config,
                    Command(resume={"decision": "resume"}),
                    resumed=True,
                )
    except asyncio.CancelledError:
        logger.info("恢复任务被取消 run_id=%s", run_id)
        await asyncio.shield(
            _finalize_canceled(
                context=context,
                content="",
                usage=_usage_dict(None),
                first_token_ms=None,
                started_at=_now(),
            )
        )
    except Exception:
        logger.exception("恢复运行失败 run_id=%s", run_id)
        await asyncio.shield(
            _finalize_failed(
                context=context,
                content="",
                usage=_usage_dict(None),
                first_token_ms=None,
                started_at=_now(),
            )
        )


async def halt_run(run_id: uuid.UUID) -> bool:
    """清理 checkpoint 里残留的 interrupt；不写 run/message、不发事件。"""
    async with get_session_factory()() as db:
        run = await db.get(Run, run_id)
        if run is None:
            return False
        start_seq = await _event_start_seq(run_id)
        publisher = RunEventPublisher(run_id, get_session_factory(), bus, start_seq=start_seq)
        context = RunContext(run_id=run_id, publisher=publisher)
        runtime = await prepare_runtime(db, run, publisher, context, resumed=True)

    async with open_checkpointer() as saver:
        graph = _build_graph_for(runtime, saver, halt=True)
        config = {"configurable": {"thread_id": str(runtime.conversation_id)}}
        snapshot = await graph.aget_state(config)
        if not snapshot.next:
            return False
        async for _ in graph.astream(
            Command(resume={"decision": "cancel"}), config, stream_mode="messages"
        ):
            pass
    return True


async def _finalize_waiting_approval(
    *,
    context: RunContext,
    runtime: RunRuntime,
) -> None:
    """图停在审批 gate：落库部分内容、run→interrupted、发非终态 run.interrupted。"""
    now = _now()
    duration_ms = elapsed_ms(runtime.started_at, now)
    content = await _redact_before_store(runtime.run_id, "".join(runtime.content_parts))
    usage = _usage_dict(runtime.usage)
    async with get_session_factory()() as db:
        run = await db.get(Run, runtime.run_id)
        if run is None:
            return
        assistant = (
            await db.get(Message, run.assistant_message_id) if run.assistant_message_id else None
        )
        if assistant is not None:
            assistant.content = content
            assistant.status = "streaming"
            assistant.citations = context.citations
        run.status = "interrupted"
        run.duration_ms = duration_ms
        run.active_duration_ms = duration_ms
        run.first_token_ms = runtime.first_token_ms
        run.input_tokens = usage["input_tokens"]
        run.output_tokens = usage["output_tokens"]
        run.reasoning_tokens = usage["reasoning_tokens"]
        conversation = await db.get(Conversation, run.conversation_id)
        if conversation is not None:
            conversation.last_message_at = now
            conversation.last_message_preview = content[:80] or "（等待审批）"
        approval_ids = list(
            (
                await db.execute(
                    select(Approval.id).where(
                        Approval.run_id == run.id, Approval.status == "pending"
                    )
                )
            ).scalars()
        )
        await db.commit()
        assistant_message_id = run.assistant_message_id

    await context.publisher.emit(
        "run.interrupted",
        {
            "run_id": str(runtime.run_id),
            "conversation_id": str(runtime.conversation_id),
            "message_id": str(assistant_message_id) if assistant_message_id else "",
            "status": "interrupted",
            "approvals": [str(item) for item in approval_ids],
            "usage": usage,
            "citations": context.citations,
        },
        event_type="run",
        status="interrupted",
        title="等待人工确认",
        duration_ms=duration_ms,
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
            # T4：同一事务内追平工作流映射行（无对应行 no-op，非工作流 run 不受影响）；
            # sync 失败即放弃本轮 finalize（同一事务，无 savepoint），由恢复链路兜底。
            await sync_workflow_run_status(db, run_id, "succeeded")
        model_name = run.model if run is not None else ""
        await db.commit()
    trace.get_current_span().set_attribute("app.run.status", "succeeded")
    record_chat_run(
        "succeeded",
        duration_ms / 1000,
        first_token_ms / 1000 if first_token_ms is not None else None,
        model_name,
    )
    return True


async def _finalize_canceled(
    *,
    context: RunContext,
    content: str,
    usage: dict[str, int],
    first_token_ms: int | None,
    started_at: datetime,
    reason: str | None = None,
) -> None:
    if context.terminal_emitted:
        return
    if await _has_terminal_event(context.run_id):
        context.terminal_emitted = True
        return
    context.terminal_emitted = True
    content = await _redact_before_store(context.run_id, content)
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
        # T4：同一事务内追平工作流映射行（无对应行 no-op）；
        # sync 失败即放弃本轮 finalize（同一事务，无 savepoint），由恢复链路兜底。
        await sync_workflow_run_status(db, run_id, "canceled")
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
            **({"reason": reason} if reason else {}),
        },
        event_type="message",
        status="canceled",
        title="审批超时已取消" if reason == "approval_timeout" else "已取消",
        duration_ms=duration_ms,
        tokens=usage,
    )
    trace.get_current_span().set_attribute("app.run.status", "canceled")
    record_chat_run(
        "canceled",
        duration_ms / 1000,
        first_token_ms / 1000 if first_token_ms is not None else None,
        model_name,
    )


async def _finalize_failed(
    *,
    context: RunContext,
    content: str,
    usage: dict[str, int],
    first_token_ms: int | None,
    started_at: datetime,
    code: str = "llm_error",
    message: str | None = None,
    retryable: bool = True,
) -> None:
    if context.terminal_emitted:
        return
    if await _has_terminal_event(context.run_id):
        context.terminal_emitted = True
        return
    context.terminal_emitted = True
    content = await _redact_before_store(context.run_id, content)
    run_id = context.run_id
    publisher = context.publisher
    now = _now()
    duration_ms = elapsed_ms(started_at, now)
    error_message = message or "模型调用失败，请稍后重试"
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
            assistant.error = {"code": code, "message": error_message, "retryable": retryable}
        run.status = "failed"
        run.error_code = code
        run.error_message = error_message
        run.finished_at = now
        run.duration_ms = duration_ms
        run.active_duration_ms = duration_ms
        run.first_token_ms = first_token_ms
        conversation = await db.get(Conversation, run.conversation_id)
        if conversation is not None:
            conversation.last_message_at = now
            conversation.last_message_preview = content[:80] or "（失败）"
            conversation.message_count += 1
        model_name = run.model
        # T4：同一事务内追平工作流映射行（无对应行 no-op）；
        # sync 失败即放弃本轮 finalize（同一事务，无 savepoint），由恢复链路兜底。
        await sync_workflow_run_status(db, run_id, "failed")
        await db.commit()

    await publisher.emit(
        "error",
        {
            "code": code,
            "message": error_message,
            "request_id": current_request_id(),
            "run_id": str(run_id),
            "retryable": retryable,
        },
        event_type="error",
        status="error",
        title="运行失败",
        error={"code": code, "message": error_message},
    )
    trace.get_current_span().set_attribute("app.run.status", "failed")
    record_chat_run(
        "failed",
        duration_ms / 1000,
        first_token_ms / 1000 if first_token_ms is not None else None,
        model_name,
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


def _sanitize_replayed_event(event_name: str, data: dict[str, Any]) -> dict[str, Any]:
    """展示层兜底：过滤历史/实时 a2a.auth_required 中的非法 authorization_url。

    远程 metadata 与历史 ``run_events`` 均不可信；任务落库前已校验，这里覆盖
    升级前已落库的旧事件以及实时帧（防御性）。只把非法值置 ``None``，不删除键、
    不写审计（展示路径不应产生审计噪音）。
    """
    if event_name != "a2a.auth_required" or "authorization_url" not in data:
        return data
    settings = get_settings()
    sanitized = dict(data)
    sanitized["authorization_url"] = validate_authorization_url(
        data.get("authorization_url"),
        env=settings.env,
        allowed_hosts=settings.a2a_auth_url_allowed_host_list,
    )
    return sanitized


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
        event_name = str((row.payload or {}).get("_event") or "")
        payload = {k: v for k, v in (row.payload or {}).items() if k != "_event"}
        payload = _sanitize_replayed_event(event_name, payload)
        error_message = None
        if isinstance(row.error, dict):
            error_message = str(row.error.get("message") or "")
        items.append(
            RunEventOut(
                id=f"{run_id}:{row.seq}",
                seq=row.seq,
                ts=row.ts,
                type=row.type,
                event=event_name,
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


async def _has_terminal_event(run_id: uuid.UUID) -> bool:
    async with get_session_factory()() as db:
        exists = await db.scalar(
            select(RunEvent.id)
            .where(
                RunEvent.run_id == run_id,
                RunEvent.payload["_event"].astext.in_(("message.done", "error")),
            )
            .limit(1)
        )
    return exists is not None


async def cancel_interrupted_run(
    run_id: uuid.UUID, *, reason: str = "approval_timeout"
) -> bool:
    """取消等待审批的 run：抢占 interrupted→canceled、写终态、发 message.done、调度 halt。"""
    now = _now()
    async with get_session_factory()() as db:
        updated = await db.execute(
            update(Run)
            .where(Run.id == run_id, Run.status == "interrupted")
            .values(status="canceled", canceled_at=now, finished_at=now)
        )
        await db.commit()
        if not updated.rowcount:
            return False
        run = await db.get(Run, run_id)
        if run is None:
            return False
        assistant = (
            await db.get(Message, run.assistant_message_id) if run.assistant_message_id else None
        )
        content = assistant.content if assistant is not None else ""
        if assistant is not None:
            assistant.status = "canceled"
        conversation = await db.get(Conversation, run.conversation_id)
        if conversation is not None:
            conversation.message_count += 1
            conversation.last_message_at = now
            conversation.last_message_preview = content[:80] or (
                "（审批超时已取消）" if reason == "approval_timeout" else "（已取消）"
            )
        canceled_approvals = await db.execute(
            update(Approval)
            .where(
                Approval.run_id == run_id,
                Approval.status.in_(("pending", "approved")),
            )
            .values(status="canceled")
        )
        for _ in range(max(0, canceled_approvals.rowcount or 0)):
            record_approval("canceled")
        duration_ms = elapsed_ms(run.started_at or run.created_at, now)
        run.duration_ms = duration_ms
        run.active_duration_ms = duration_ms
        await db.commit()
        message_id = run.assistant_message_id
        model_name = run.model
        citations = assistant.citations if assistant is not None else []
        usage = _usage_dict(
            {
                "input_tokens": run.input_tokens,
                "output_tokens": run.output_tokens,
                "reasoning_tokens": run.reasoning_tokens,
            }
        )

    await _wait_run_task(run_id)
    terminal_emitted = False
    if not await _has_terminal_event(run_id):
        terminal_emitted = True
        publisher = RunEventPublisher(
            run_id, get_session_factory(), bus, start_seq=await _event_start_seq(run_id)
        )
        await publisher.emit(
            "message.done",
            {
                "message_id": str(message_id) if message_id else "",
                "usage": usage,
                "finish_reason": "canceled",
                "model": model_name,
                "first_token_ms": None,
                "duration_ms": duration_ms,
                "active_duration_ms": duration_ms,
                "citations": citations,
                "artifacts": [],
                "reason": reason,
            },
            event_type="message",
            status="canceled",
            title="审批超时已取消" if reason == "approval_timeout" else "已取消",
            duration_ms=duration_ms,
            tokens=usage,
        )
    if terminal_emitted:
        record_chat_run("canceled", duration_ms / 1000, None, model_name)
    try:
        await halt_run(run_id)
    except Exception:
        logger.exception("清理挂起线程失败 run_id=%s", run_id)
    return True


async def fail_orphaned_run(
    run_id: uuid.UUID,
    *,
    code: str = "run_interrupted",
    message: str = "服务重启导致运行中断，请重试",
) -> bool:
    """单实例启动/巡检发现无人认领的 run：写失败终态，避免会话永久占锁。"""
    async with get_session_factory()() as db:
        # 条件 UPDATE 抢占：只有仍处于 queued/running 的 run 才允许被恢复失败，
        # 避免周期巡检把已成功/已取消的 run 覆盖成 failed。
        claimed = await db.execute(
            update(Run)
            .where(Run.id == run_id, Run.status.in_(("queued", "running")))
            .values(status="failed", error_code=code, finished_at=_now())
        )
        await db.commit()
        if not claimed.rowcount:
            return False
        run = await db.get(Run, run_id)
        if run is None:
            return False
        assistant = (
            await db.get(Message, run.assistant_message_id) if run.assistant_message_id else None
        )
        content = assistant.content if assistant is not None else ""
        usage = _usage_dict(
            {
                "input_tokens": run.input_tokens,
                "output_tokens": run.output_tokens,
                "reasoning_tokens": run.reasoning_tokens,
            }
        )
        started_at = run.started_at or run.created_at
        first_token_ms = run.first_token_ms
    if await _has_terminal_event(run_id):
        return False

    publisher = RunEventPublisher(
        run_id, get_session_factory(), bus, start_seq=await _event_start_seq(run_id)
    )
    context = RunContext(run_id=run_id, publisher=publisher)
    await _finalize_failed(
        context=context,
        content=content,
        usage=usage,
        first_token_ms=first_token_ms,
        started_at=started_at,
        code=code,
        message=message,
        retryable=True,
    )
    async with get_session_factory()() as db:
        await db.execute(
            update(Approval)
            .where(Approval.run_id == run_id, Approval.status.in_(("pending", "approved")))
            .values(status="canceled")
        )
        await write_audit(
            db,
            actor_type="system",
            actor_id="recovery",
            action="run.recovered_failed",
            resource_type="run",
            resource_id=str(run_id),
            detail={"reason": code},
        )
        await db.commit()
    return True


async def cancel_run(db: AsyncSession, user: User, run_id: uuid.UUID) -> RunCancelResult:
    run = await _load_owned_run(db, user, run_id)
    if run.status not in _ACTIVE_STATUSES:
        raise RunNotCancelableError(run.status)
    now = _now()
    if run.status == "interrupted":
        # 等待审批中的 run 没有活跃任务：直接抢占状态、写终态并清理挂起线程。
        if not await cancel_interrupted_run(run_id, reason="user_canceled"):
            raise RunNotCancelableError(run.status)
        return RunCancelResult(id=run_id, status="canceled", canceled_at=now)
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
    # 兜底：任务收尾后若 checkpoint 仍挂起（取消与挂起竞态），清理线程。
    _spawn_background(halt_run(run_id))
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


async def ensure_replay_size(
    db: AsyncSession,
    run_id: uuid.UUID,
    after_seq: int,
    *,
    max_replay: int = SSE_MAX_REPLAY,
) -> None:
    """SSE 续流前的回放上限检查：超限时提示改用分页接口或更接近的 after_seq。"""
    count = await db.scalar(
        select(func.count())
        .select_from(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
    )
    if int(count or 0) > max_replay:
        # HTTP 状态码只能在建流前决定；生成器内部另有 limit(max_replay + 1) 兜底。
        raise BadRequestError(
            f"回放事件超过 {max_replay} 条，请使用事件分页接口分批回放，"
            "或传入更接近的 after_seq 后重试",
            code="replay_too_large",
        )


async def stream_run_events(
    run_id: uuid.UUID,
    *,
    after_seq: int = 0,
    max_replay: int | None = None,
    close_terminal_when_replayed: bool = False,
) -> AsyncIterator[str]:
    """SSE 生成器：包一层 ``sse.stream`` span，内部回放 + 实时跟随。

    ``max_replay`` 与 ``close_terminal_when_replayed`` 默认关闭，保持旧
    ``POST /chat/stream`` 的语义；续流端点显式开启回放上限与终态早关。
    """
    attributes: dict[str, Any] = {
        "app.run_id": str(run_id),
        "app.sse.after_seq": after_seq,
    }
    if max_replay is not None:
        attributes["app.sse.max_replay"] = max_replay
    span = get_tracer().start_span("sse.stream", attributes=attributes)
    events = 0
    try:
        async for frame in _stream_run_events_inner(
            run_id,
            after_seq=after_seq,
            max_replay=max_replay,
            close_terminal_when_replayed=close_terminal_when_replayed,
        ):
            events += 1
            yield frame
    finally:
        span.set_attribute("app.sse.events", events)
        span.end()


async def _stream_run_events_inner(
    run_id: uuid.UUID,
    *,
    after_seq: int = 0,
    max_replay: int | None = None,
    close_terminal_when_replayed: bool = False,
) -> AsyncIterator[str]:
    """SSE 生成器：先回放已落库事件，再跟随实时事件。"""
    settings = get_settings()
    queue = await bus.subscribe(run_id)
    last_seq = after_seq
    # SSE 请求内产生的日志都带 run_id，便于和后台 run 任务串起来。
    run_token = run_id_ctx.set(str(run_id))
    try:
        async with get_session_factory()() as db:
            run_status: str | None = None
            if close_terminal_when_replayed:
                # 先读 run 状态再读事件：缩小「状态已终态、终态事件刚提交」的漏帧窗口。
                run = await db.get(Run, run_id)
                run_status = run.status if run is not None else None
            stmt = (
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.seq > last_seq)
                .order_by(RunEvent.seq.asc())
            )
            if max_replay is not None:
                # 多取一条用于判断是否超限；生成器启动后无法再改状态码，
                # 超限时只发前 max_replay 条并关闭（路由层已提前返回 400）。
                stmt = stmt.limit(max_replay + 1)
            rows = list((await db.execute(stmt)).scalars().all())
        truncated = max_replay is not None and len(rows) > max_replay
        replay_rows = rows if max_replay is None else rows[:max_replay]
        for row in replay_rows:
            if row.seq <= last_seq:
                continue
            event_name = str((row.payload or {}).get("_event") or "")
            if not event_name:
                continue
            data = {k: v for k, v in (row.payload or {}).items() if k != "_event"}
            data = _sanitize_replayed_event(event_name, data)
            last_seq = row.seq
            yield format_sse(row.seq, event_name, data)
            if event_name in TERMINAL_EVENTS:
                return
        if truncated:
            return
        if close_terminal_when_replayed and (
            run_status is None or run_status in TERMINAL_RUN_STATUSES
        ):
            # 已结束（或已被删除）的 run 不会再有终态事件：回放完立即关闭，
            # 避免空流挂到空闲超时；interrupted 不在终态集合内，仍需保持连接等审批恢复。
            return

        idle_seconds = 0
        heartbeat_seconds = _SSE_HEARTBEAT_SECONDS
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
            data = _sanitize_replayed_event(event_name, data)
            yield format_sse(seq, event_name, data)
            if event_name in TERMINAL_EVENTS:
                return
    finally:
        run_id_ctx.reset(run_token)
        await bus.unsubscribe(run_id, queue)
