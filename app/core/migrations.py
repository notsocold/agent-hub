"""Alembic 迁移版本辅助：供就绪检查比对数据库与脚本 head。"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.db.session import get_engine

logger = logging.getLogger(__name__)

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


async def ensure_migrations_current(settings: Settings | None = None) -> None:
    """启动期校验（可选）：数据库迁移版本必须等于脚本 head，否则拒绝启动。"""
    settings = settings or get_settings()
    if not settings.readiness_check_migrations:
        return
    expected = expected_migration_head()
    if expected is None:
        raise RuntimeError("无法读取 Alembic 迁移脚本 head")
    try:
        async with get_engine().connect() as conn:
            current = (
                await conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
            ).scalar_one_or_none()
    except Exception as exc:
        raise RuntimeError("数据库未执行 alembic upgrade head") from exc
    if current != expected:
        raise RuntimeError(f"数据库迁移版本 {current or '空'} != 脚本 head {expected}")


@lru_cache(maxsize=1)
def expected_migration_head() -> str | None:
    """返回脚本目录的当前 head；读取失败返回 None（就绪检查据此报错）。"""
    try:
        config = Config(str(_ALEMBIC_INI))
        script = ScriptDirectory.from_config(config)
        return script.get_current_head()
    except Exception:
        logger.exception("读取 Alembic head 失败")
        return None


__all__ = ["ensure_migrations_current", "expected_migration_head"]
