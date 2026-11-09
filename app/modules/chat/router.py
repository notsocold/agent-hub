"""聊天与运行路由。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

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


@router.post("/chat/stream")
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
