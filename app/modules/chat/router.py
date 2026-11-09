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
    "**帧格式**：`id: <seq>`、`event: <事件名>`、`data: <JSON>`，心跳为注释帧 `: ping`"
    "（每 15 秒，不占 `seq`）。`seq` 从 1 开始单调递增；`message.delta` 不落库，"
    "事件回放的 `seq` 允许跳号。\n\n"
    "**幂等**：相同 `conversation_id + client_message_id` 再次请求时返回同一 run 的"
    "事件回放（HTTP 200，`X-Run-ID` 不变，`seq` 从 1 开始），不会创建新消息。\n\n"
    "**断开**：客户端断开不会取消运行；用 `GET /runs/{id}` 与 "
    "`GET /runs/{id}/events?after_seq=` 恢复。M1 不保证 `message.delta` 的部分文本恢复。\n\n"
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


@router.post("/runs/{run_id}/cancel", response_model=RunCancelResult)
async def cancel_run(run_id: uuid.UUID, user: CurrentUser, db: DbSession) -> RunCancelResult:
    return await service.cancel_run(db, user, run_id)


@router.post("/runs/{run_id}/resume", response_model=RunResumeResult)
async def resume_run(
    run_id: uuid.UUID, payload: RunResumeRequest, user: CurrentUser, db: DbSession
) -> RunResumeResult:
    return await service.resume_run(db, user, run_id, payload.input)
