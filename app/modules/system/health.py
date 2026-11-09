"""健康检查。"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import get_engine

logger = logging.getLogger(__name__)

router = APIRouter(tags=["系统"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "app": get_settings().app_name}


@router.get("/readyz")
async def readyz() -> JSONResponse:
    components: list[dict[str, object]] = []
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        components.append({"name": "database", "status": "ok"})
        status_code = 200
        overall = "ok"
    except Exception as exc:
        logger.warning("数据库就绪检查失败：%s", exc)
        components.append({"name": "database", "status": "error"})
        status_code = 503
        overall = "error"
    return JSONResponse(
        status_code=status_code,
        content={"status": overall, "components": components},
    )
