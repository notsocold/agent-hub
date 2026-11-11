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
from app.core.metrics import setup_metrics
from app.core.middleware import RequestContextMiddleware
from app.core.migrations import ensure_migrations_current
from app.core.observability import ObservabilityMiddleware
from app.core.telemetry import configure_tracing, shutdown_tracing
from app.db.session import dispose_engine, get_session_factory
from app.modules.approvals.service import expire_pending
from app.modules.chat.registry import registry
from app.modules.maintenance.cleanup import cleanup_sweeper
from app.modules.maintenance.recovery import recover, recovery_sweeper
from app.modules.skills.service import sync_builtin_skills
from app.modules.system.health import router as health_router
from app.modules.system.metrics_router import metrics_endpoint

logger = logging.getLogger(__name__)


async def _approval_sweeper() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            canceled = await expire_pending()
            if canceled:
                logger.info("审批超时巡检：%s 个 run 已取消", len(canceled))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("审批超时巡检失败")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.draining = False
    configure_logging(settings)
    configure_tracing(settings)
    setup_metrics(settings)
    settings.validate_runtime()
    await ensure_migrations_current(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.attachment_dir.mkdir(parents=True, exist_ok=True)
    if settings.knowledge_enabled:
        settings.knowledge_dir.mkdir(parents=True, exist_ok=True)
    await ensure_checkpoint_ready(settings)
    try:
        async with get_session_factory()() as db:
            changed = await sync_builtin_skills(db)
            if changed:
                logger.info("系统技能同步：%s 项变更", changed)
    except Exception:
        logger.exception("系统技能同步失败（不阻塞启动）")
    if settings.startup_recovery_enabled:
        try:
            await recover(startup=True)
        except Exception:
            logger.exception("启动恢复失败（不阻塞启动）")
    recovery_task: asyncio.Task[None] | None = None
    if settings.startup_recovery_enabled and settings.recovery_interval_seconds > 0:
        recovery_task = asyncio.create_task(
            recovery_sweeper(settings.recovery_interval_seconds), name="recovery-sweeper"
        )
    sweeper = asyncio.create_task(_approval_sweeper(), name="approval-sweeper")
    cleanup_task: asyncio.Task[None] | None = None
    if settings.cleanup_enabled:
        cleanup_task = asyncio.create_task(
            cleanup_sweeper(settings.cleanup_interval_seconds), name="cleanup-sweeper"
        )
    logger.info("agent-hub 启动完成 env=%s", settings.env)
    try:
        yield
    finally:
        app.state.draining = True
        sweeper.cancel()
        if recovery_task is not None:
            recovery_task.cancel()
        if cleanup_task is not None:
            cleanup_task.cancel()
        await asyncio.gather(
            sweeper,
            *([recovery_task] if recovery_task is not None else []),
            *([cleanup_task] if cleanup_task is not None else []),
            return_exceptions=True,
        )
        drained = await registry.drain(settings.shutdown_grace_seconds)
        if not drained:
            logger.warning(
                "排水超时（%ss），取消剩余 run", settings.shutdown_grace_seconds
            )
        await registry.shutdown(settings.shutdown_finalize_timeout_seconds)
        await shutdown_tracing()
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
    # add_middleware 每次插到最外层；按「内 → 外」顺序添加：
    # Observability 需要 request_id 已生成，RequestContext 在其外层；
    # CORS 放最外层，保证预检与错误响应也带 CORS 头。
    app.add_middleware(ObservabilityMiddleware, metrics_path=settings.metrics_path)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Run-ID", "X-Trace-ID"],
    )
    register_exception_handlers(app)
    app.include_router(health_router)
    if settings.metrics_enabled:
        app.add_api_route(
            settings.metrics_path,
            metrics_endpoint,
            methods=["GET"],
            include_in_schema=False,
        )
    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
