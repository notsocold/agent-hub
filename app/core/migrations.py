"""Alembic 迁移版本辅助：供就绪检查比对数据库与脚本 head。"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

logger = logging.getLogger(__name__)

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


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


__all__ = ["expected_migration_head"]
