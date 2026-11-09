"""审批路由。"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Query

from app.core.deps import CurrentUser, DbSession
from app.modules.approvals import service
from app.modules.approvals.schemas import (
    ApprovalDecisionRequest,
    ApprovalDecisionResult,
    ApprovalOut,
    ApprovalPage,
)

router = APIRouter(prefix="/approvals", tags=["审批"])


@router.get("", response_model=ApprovalPage)
async def list_approvals(
    user: CurrentUser,
    db: DbSession,
    status: Literal["pending", "approved", "rejected", "expired", "canceled", "all"] = "pending",
    conversation_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
) -> ApprovalPage:
    return await service.list_approvals(
        db,
        user,
        status=status,
        conversation_id=conversation_id,
        run_id=run_id,
        limit=limit,
        cursor=cursor,
    )


@router.get("/{approval_id}", response_model=ApprovalOut)
async def get_approval(approval_id: uuid.UUID, user: CurrentUser, db: DbSession) -> ApprovalOut:
    return await service.get_approval(db, user, approval_id)


@router.post("/{approval_id}/decision", response_model=ApprovalDecisionResult)
async def decide(
    approval_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    user: CurrentUser,
    db: DbSession,
) -> ApprovalDecisionResult:
    return await service.decide(db, user, approval_id, payload)
