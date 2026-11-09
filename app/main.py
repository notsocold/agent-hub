"""FastAPI 应用入口。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.agents.checkpointer import ensure_checkpoint_ready
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.db.session import dispose_engine
from app.modules.approvals.service import expire_pending
from app.modules.chat.registry import registry
from app.modules.system.health import router as health_router

logger = logging.getLogger(__name__)


async def _approval_sweeper() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            expired = await expire_pending()
            if expired:
                logger.info("审批超时巡检：%s 条置为 expired", expired)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("审批超时巡检失败")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.debug)
    settings.validate_runtime()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    await ensure_checkpoint_ready(settings)
    sweeper = asyncio.create_task(_approval_sweeper(), name="approval-sweeper")
    logger.info("agent-hub 启动完成 env=%s", settings.env)
    try:
        yield
    finally:
        sweeper.cancel()
        await asyncio.gather(sweeper, return_exceptions=True)
        await registry.shutdown()
        await dispose_engine()
        logger.info("agent-hub 已停止")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Agent Hub API",
        version=__version__,
        description="面向用户的多智能体对话服务：LangGraph 编排 + A2A + pgvector",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Run-ID"],
    )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
