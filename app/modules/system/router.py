"""系统信息路由。"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import CurrentUser
from app.modules.system import service
from app.modules.system.schemas import SystemInfoOut

router = APIRouter(prefix="/system", tags=["系统"])


@router.get("/info", response_model=SystemInfoOut)
async def system_info(_: CurrentUser) -> SystemInfoOut:
    return service.build_system_info()
