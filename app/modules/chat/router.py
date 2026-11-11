"""聊天与运行路由。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.api.schemas import ErrorEnvelopeOut
from app.core.deps import CurrentUser, DbSession
from app.core.errors import current_request_id
from app.modules.chat import service
from app.modules.chat.schemas import (
    ChatStreamRequest,
    RunCancelResult,
    RunEventPage,
    RunOut,
    RunResumeRequest,
    RunResumeResult,
)

router = APIRouter(tags=["聊天与运行"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

_SSE_DESCRIPTION = (
    "Server-Sent Events 流，`Content-Type: text/event-stream`。\n\n"
    "**事件顺序**：`conversation.created`（仅新会话）→ `run.started` → "
    "`message.created` → `message.started` → `message.delta`（0..n）→ `message.done`；"
    "致命错误只发送 `error` 并关闭流。\n\n"
    "**HITL（M5）**：需要审批时插入 `approval.required` → `run.interrupted`（非终态，"
    "SSE 保持连接，空闲 15 分钟后关闭）；审批决定后 `run.resumed` → 后续 `message.delta` → "
    "`message.done`。审批超时取消时 `message.done.finish_reason=canceled`、`reason=approval_timeout`。\n\n"
    "**帧格式**：`id: <seq>`、`event: <事件名>`、`data: <JSON>`，心跳为注释帧 `: ping`"
    "（每 15 秒，不占 `seq`）。`seq` 从 1 开始单调递增；`message.delta` 不落库，"
    "事件回放的 `seq` 允许跳号。\n\n"
    "**幂等**：相同 `conversation_id + client_message_id` 再次请求时返回同一 run 的"
    "事件回放（HTTP 200，`X-Run-ID` 不变，`seq` 从 1 开始），不会创建新消息。\n\n"
    "**断开**：客户端断开不会取消运行；用 `GET /runs/{id}` 与 "
    "`GET /runs/{id}/events?after_seq=` 分页回放已落库事件（不跟随实时流），"
    "或改用 SSE 续流端点 `GET /runs/{id}/stream?after_seq=` 从断点继续跟随；"
    "已发出的 delta 不回溯，挂起时已产出的内容会落库。\n\n"
    "**事件 data 关键字段**：`conversation.created` → `conversation_id`、`title`；"
    "`run.started` → `run_id`、`conversation_id`、`user_message_id`、`client_message_id`、"
    "`model`、`reasoning_effort`；`message.created` → `message_id`、`role`；"
    "`message.started` → `message_id`；`message.delta` → `message_id`、`delta`；"
    "`message.done` → `run_id`、`message_id`、`conversation_id`、`usage`、`finish_reason`、"
    "`model`、`first_token_ms`、`duration_ms`、`active_duration_ms`、`citations`、"
    "`artifacts`、`reasoning`；`error` → `code`、`message`、`request_id`。"
    "完整字段与示例见 `docs/api.md` §3.3、§7.3、§7.9。"
)

_SSE_RESPONSES: dict[int | str, dict] = {
    200: {
        "description": _SSE_DESCRIPTION,
        "content": {
            "text/event-stream": {
                "schema": {"type": "string"},
                "example": (
                    "id: 1\n"
                    "event: conversation.created\n"
                    'data: {"run_id":"...","seq":1,"conversation_id":"..."}\n\n'
                    "id: 2\n"
                    "event: run.started\n"
                    'data: {"run_id":"...","seq":2,"conversation_id":"...",'
                    '"model":"deepseek-chat","reasoning_effort":"low"}\n\n'
                    "id: 3\n"
                    "event: message.created\n"
                    'data: {"run_id":"...","seq":3,"message_id":"..."}\n\n'
                ),
            }
        },
        "headers": {
            "X-Run-ID": {
                "description": "本次（或幂等重放命中的）运行 ID，建连时即返回",
                "schema": {"type": "string", "format": "uuid"},
            },
            "X-Request-ID": {
                "description": "请求 ID，错误响应体中的 request_id 与之一致",
                "schema": {"type": "string"},
            },
        },
    },
    403: {
        "model": ErrorEnvelopeOut,
        "description": "能力未启用（`capability_disabled`）：请求带上 `skills`、`mcp_servers`、`mcp_tools`、`knowledge_categories` 或 `attachments`",
    },
    409: {
        "model": ErrorEnvelopeOut,
        "description": "`run_in_progress`（同会话已有未结束运行，`details.run_id` 指向占锁运行）或 `workflow_disabled`（M1 未实现工作流）",
    },
    422: {
        "model": ErrorEnvelopeOut,
        "description": "`validation_error`（参数不合法）或 `model_not_allowed`（模型不在白名单）",
    },
}

_RUN_STREAM_DESCRIPTION = (
    "运行事件 SSE 续流端点，`Content-Type: text/event-stream`。\n\n"
    "**鉴权**：需要 `Authorization: Bearer <access token>`；非本人或不存在统一 404（不泄露存在性）。"
    "浏览器 `EventSource` 不支持自定义请求头，请用 `fetch`/`@microsoft/fetch-event-source` 携带 Bearer。\n\n"
    "**续流**：先回放 `seq > after_seq` 的已落库事件（`after_seq` 默认 0），"
    "再订阅事件总线跟随实时事件；回放超过 1000 条返回 400 `replay_too_large`，"
    "请改用 `GET /runs/{id}/events?after_seq=` 分页回放，或传入更接近的 `after_seq`。\n\n"
    "**关闭**：收到终态事件（`message.done`/`error`）后立即关闭；run 已结束"
    "（`succeeded`/`failed`/`canceled`）且回放里没有终态事件时，回放完即关闭。"
    "`interrupted` 的 run 保持连接等待审批恢复，空闲超过 `HITL_STREAM_TIMEOUT_SECONDS` 后关闭。\n\n"
    "**重连**：若流以 EOF 结束且尚未收到终态事件，请携带最后收到事件的 `seq` 作为 "
    "`after_seq` 重连，补齐可能仍在落库的终态事件。\n\n"
    "**心跳**：等待实时事件期间每 15 秒发送注释帧 `: ping`（不占 `seq`）。\n\n"
    "**部署**：事件总线为进程内实现（单实例）；多实例部署时实时帧可能丢失，"
    "重连时请携带 `after_seq` 通过回放补齐。"
)


@router.post("/chat/stream", response_class=StreamingResponse, responses=_SSE_RESPONSES)
async def chat_stream(
    payload: ChatStreamRequest, user: CurrentUser, db: DbSession
) -> StreamingResponse:
    result = await service.start_chat(db, user, payload)
    headers: dict[str, str] = {
        **_SSE_HEADERS,
        "X-Run-ID": str(result.run_id),
        "X-Request-ID": current_request_id(),
    }
    return StreamingResponse(
        service.stream_run_events(result.run_id),
        media_type="text/event-stream",
        headers=headers,
    )


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: uuid.UUID, user: CurrentUser, db: DbSession) -> RunOut:
    return await service.get_run(db, user, run_id)


@router.get("/runs/{run_id}/events", response_model=RunEventPage)
async def list_run_events(
    run_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=100, ge=1, le=500),
    after_seq: int | None = Query(default=None, ge=0),
    types: str | None = Query(default=None),
    q: str | None = Query(default=None, min_length=1, max_length=100),
    cursor: str | None = None,
) -> RunEventPage:
    return await service.list_run_events(
        db,
        user,
        run_id,
        limit=limit,
        after_seq=after_seq,
        types=types,
        q=q,
        cursor=cursor,
    )


@router.get(
    "/runs/{run_id}/stream",
    response_class=StreamingResponse,
    responses=_SSE_RESPONSES,
    description=_RUN_STREAM_DESCRIPTION,
)
async def stream_run(
    run_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    after_seq: int = Query(default=0, ge=0, le=2**31 - 1),
) -> StreamingResponse:
    # 所有权校验先于建流与回放上限：非本人/不存在统一 404，不泄露存在性。
    await service.get_run(db, user, run_id)
    await service.ensure_replay_size(
        db, run_id, after_seq, max_replay=service.SSE_MAX_REPLAY
    )
    headers: dict[str, str] = {
        **_SSE_HEADERS,
        "X-Run-ID": str(run_id),
        "X-Request-ID": current_request_id(),
    }
    return StreamingResponse(
        service.stream_run_events(
            run_id,
            after_seq=after_seq,
            max_replay=service.SSE_MAX_REPLAY,
            close_terminal_when_replayed=True,
        ),
        media_type="text/event-stream",
        headers=headers,
    )


@router.post("/runs/{run_id}/cancel", response_model=RunCancelResult)
async def cancel_run(run_id: uuid.UUID, user: CurrentUser, db: DbSession) -> RunCancelResult:
    return await service.cancel_run(db, user, run_id)


@router.post("/runs/{run_id}/resume", response_model=RunResumeResult)
async def resume_run(
    run_id: uuid.UUID, payload: RunResumeRequest, user: CurrentUser, db: DbSession
) -> RunResumeResult:
    return await service.resume_run(db, user, run_id, payload.input)
