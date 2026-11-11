"""日志上下文：把 run / conversation 标识注入日志字段。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

run_id_ctx: ContextVar[str] = ContextVar("run_id", default="")
conversation_id_ctx: ContextVar[str] = ContextVar("conversation_id", default="")


@contextmanager
def bind_run_context(
    run_id: str | uuid.UUID,
    conversation_id: str | uuid.UUID | None = None,
) -> Iterator[None]:
    """在一个代码块内绑定 run / conversation，退出时恢复原值。"""
    run_token = run_id_ctx.set(str(run_id))
    conversation_token = (
        conversation_id_ctx.set(str(conversation_id)) if conversation_id is not None else None
    )
    try:
        yield
    finally:
        if conversation_token is not None:
            conversation_id_ctx.reset(conversation_token)
        run_id_ctx.reset(run_token)


__all__ = ["bind_run_context", "conversation_id_ctx", "run_id_ctx"]
