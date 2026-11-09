"""``/api/v1`` 聚合路由。"""

from __future__ import annotations

from fastapi import APIRouter

from app.modules.approvals.router import router as approvals_router
from app.modules.auth.router import router as auth_router
from app.modules.chat.router import router as chat_router
from app.modules.conversations.router import router as conversations_router
from app.modules.system.router import router as system_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(conversations_router)
api_router.include_router(chat_router)
api_router.include_router(approvals_router)
api_router.include_router(system_router)
