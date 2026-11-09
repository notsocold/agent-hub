"""指令路由。"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import CurrentUser, DbSession
from app.modules.instructions import service
from app.modules.instructions.schemas import (
    InstructionOut,
    InstructionUpdateRequest,
    InstructionUpdateResult,
)

router = APIRouter(prefix="/instructions", tags=["指令"])


@router.get("", response_model=InstructionOut)
async def get_instructions(user: CurrentUser, db: DbSession) -> InstructionOut:
    return await service.get_effective(db, user)


@router.put("", response_model=InstructionUpdateResult)
async def update_instructions(
    payload: InstructionUpdateRequest, user: CurrentUser, db: DbSession
) -> InstructionUpdateResult:
    return await service.update_instruction(db, user, payload)
