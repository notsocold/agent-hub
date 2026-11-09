"""异步数据库引擎与会话工厂。

引擎懒加载，避免 import 阶段连接数据库（测试可以替换配置后首次使用）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        kwargs: dict[str, Any] = {
            "echo": settings.db_echo,
            "pool_pre_ping": settings.db_pool_pre_ping,
        }
        # SQLite 测试时不支持连接池参数；生产默认走 PostgreSQL。
        if not settings.database_url.startswith("sqlite"):
            kwargs["pool_size"] = settings.db_pool_size
            kwargs["max_overflow"] = settings.db_max_overflow
            # 契约要求时间统一返回 UTC；把数据库会话时区固定为 UTC，
            # 避免 timestamptz 按本机时区返回 +08:00。
            kwargs["connect_args"] = {"options": "-c timezone=UTC"}
        _engine = create_async_engine(settings.database_url, **kwargs)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(), expire_on_commit=False, autoflush=False
        )
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每个请求一个会话，异常时回滚。"""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
