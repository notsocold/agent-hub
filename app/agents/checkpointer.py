"""LangGraph checkpoint 生命周期。

生产用 PostgresSaver（多实例共享状态），测试用 InMemorySaver。
checkpoint 表由框架 ``setup()`` 创建，不进 Alembic。
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.db.session import get_engine

logger = logging.getLogger(__name__)

_SCHEMA_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")
_memory_saver: InMemorySaver | None = None


def _validate_schema(schema: str) -> str:
    if not _SCHEMA_PATTERN.match(schema):
        raise RuntimeError(f"非法 checkpoint schema：{schema}")
    return schema


def _get_memory_saver() -> InMemorySaver:
    global _memory_saver
    if _memory_saver is None:
        _memory_saver = InMemorySaver()
    return _memory_saver


def reset_memory_checkpointer() -> None:
    """测试辅助：清空内存 checkpoint，避免用例互相影响。"""
    global _memory_saver
    _memory_saver = InMemorySaver()


async def ensure_checkpoint_ready(settings: Settings | None = None) -> None:
    if (settings or get_settings()).checkpoint_backend != "postgres":
        return
    settings = settings or get_settings()
    schema = _validate_schema(settings.checkpoint_schema)
    async with get_engine().begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    async with AsyncPostgresSaver.from_conn_string(settings.checkpoint_dsn) as saver:
        await saver.setup()
    logger.info("LangGraph checkpoint 已就绪 schema=%s", schema)


@asynccontextmanager
async def open_checkpointer() -> AsyncIterator[BaseCheckpointSaver]:
    """每次图执行打开一个 checkpoint 连接；单次运行内复用。"""
    settings = get_settings()
    if settings.checkpoint_backend == "memory":
        yield _get_memory_saver()
        return
    async with AsyncPostgresSaver.from_conn_string(settings.checkpoint_dsn) as saver:
        yield saver
