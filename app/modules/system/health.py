"""健康检查与就绪检查。

``/healthz`` 是纯存活探针（不查依赖）；``/readyz`` 检查数据库、迁移版本、
checkpoint schema 与本地存储，关键组件失败或正在排水时返回 503。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import __version__
from app.core.config import get_settings
from app.core.migrations import expected_migration_head
from app.db.session import get_engine

logger = logging.getLogger(__name__)

router = APIRouter(tags=["系统"])

_CRITICAL_COMPONENTS = frozenset({"database", "migrations", "checkpoint"})


async def _check_database(timeout: float) -> dict[str, Any]:
    del timeout
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("数据库就绪检查失败：%s", exc)
        return {"name": "database", "status": "error", "detail": "数据库不可用"}
    return {"name": "database", "status": "ok"}


async def _check_migrations(timeout: float) -> dict[str, Any]:
    del timeout
    settings = get_settings()
    if not settings.readiness_check_migrations:
        return {"name": "migrations", "status": "skipped"}
    expected = expected_migration_head()
    if expected is None:
        return {"name": "migrations", "status": "error", "detail": "无法读取迁移脚本 head"}
    try:
        async with get_engine().connect() as conn:
            current = (
                await conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
            ).scalar_one_or_none()
    except Exception as exc:
        logger.warning("迁移版本检查失败：%s", exc)
        return {
            "name": "migrations",
            "status": "error",
            "detail": "未执行 alembic upgrade head",
        }
    if current != expected:
        return {
            "name": "migrations",
            "status": "error",
            "detail": f"数据库 {current or '空'} != 脚本 {expected}",
        }
    return {"name": "migrations", "status": "ok", "detail": f"head={expected}"}


async def _check_checkpoint(timeout: float) -> dict[str, Any]:
    del timeout
    settings = get_settings()
    if settings.checkpoint_backend == "memory":
        return {"name": "checkpoint", "status": "skipped"}
    try:
        async with get_engine().connect() as conn:
            exists = await conn.scalar(
                text("SELECT to_regclass(:name)"),
                {"name": f'"{settings.checkpoint_schema}".checkpoints'},
            )
    except Exception as exc:
        logger.warning("checkpoint 就绪检查失败：%s", exc)
        return {"name": "checkpoint", "status": "error", "detail": "checkpoint 不可用"}
    if exists is None:
        return {"name": "checkpoint", "status": "error", "detail": "checkpoint 表不存在"}
    return {"name": "checkpoint", "status": "ok"}


async def _check_storage(timeout: float) -> dict[str, Any]:
    del timeout
    settings = get_settings()
    if settings.storage_backend != "local":
        return {"name": "storage", "status": "skipped"}
    paths = [settings.data_dir]
    if settings.knowledge_enabled:
        paths.append(settings.knowledge_dir)
    if settings.attachments_enabled:
        paths.append(settings.attachment_dir)
    missing = [str(path) for path in paths if not path.exists() or not os.access(path, os.W_OK)]
    if missing:
        return {
            "name": "storage",
            "status": "error",
            "detail": f"目录不可写：{', '.join(missing)}",
        }
    return {"name": "storage", "status": "ok"}


async def _safe_check(name: str, check, timeout: float) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(check(timeout), timeout=timeout)
    except TimeoutError:
        return {"name": name, "status": "error", "detail": "超时"}
    except Exception as exc:
        logger.warning("就绪检查异常 name=%s：%s", name, exc)
        return {"name": name, "status": "error", "detail": str(exc)}


async def readiness_components() -> list[dict[str, Any]]:
    settings = get_settings()
    timeout = settings.readiness_timeout_seconds
    checks = (
        ("database", _check_database),
        ("migrations", _check_migrations),
        ("checkpoint", _check_checkpoint),
        ("storage", _check_storage),
    )
    return list(
        await asyncio.gather(
            *(_safe_check(name, check, timeout) for name, check in checks)
        )
    )


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": __version__,
        "build": settings.build_label,
        "env": settings.env,
    }


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    settings = get_settings()
    draining = bool(getattr(request.app.state, "draining", False))
    components = await readiness_components()
    failed_critical = any(
        item["status"] == "error" and item["name"] in _CRITICAL_COMPONENTS for item in components
    )
    degraded = any(item["status"] == "error" for item in components)
    if draining or failed_critical:
        overall = "error"
        status_code = 503
    elif degraded:
        overall = "degraded"
        status_code = 200
    else:
        overall = "ok"
        status_code = 200
    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall,
            "version": __version__,
            "build": settings.build_label,
            "env": settings.env,
            "draining": draining,
            "components": components,
        },
    )


__all__ = ["healthz", "readiness_components", "readyz", "router"]
