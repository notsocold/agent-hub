"""运行任务注册表：取消与进程内生命周期管理。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from app.modules.chat.events import RunEventPublisher

logger = logging.getLogger(__name__)


@dataclass
class RunContext:
    run_id: uuid.UUID
    publisher: RunEventPublisher
    task: asyncio.Task[None] | None = None
    cancel_requested: bool = field(default=False)
    # 终态事件只能发一次：成功/取消/失败竞态时避免重复 message.done。
    terminal_emitted: bool = field(default=False)


class RunRegistry:
    def __init__(self) -> None:
        self._contexts: dict[uuid.UUID, RunContext] = {}

    def register(self, context: RunContext) -> None:
        self._contexts[context.run_id] = context

    def start(self, context: RunContext, coroutine) -> None:
        context.task = asyncio.create_task(coroutine, name=f"run-{context.run_id}")
        self._contexts[context.run_id] = context
        context.task.add_done_callback(lambda _: self._contexts.pop(context.run_id, None))

    def get(self, run_id: uuid.UUID) -> RunContext | None:
        return self._contexts.get(run_id)

    def is_running(self, run_id: uuid.UUID) -> bool:
        context = self._contexts.get(run_id)
        return bool(context and context.task and not context.task.done())

    def cancel(self, run_id: uuid.UUID) -> bool:
        context = self._contexts.get(run_id)
        if context is None or context.task is None or context.task.done():
            return False
        context.cancel_requested = True
        context.task.cancel()
        return True

    async def shutdown(self) -> None:
        tasks = [
            context.task
            for context in self._contexts.values()
            if context.task is not None and not context.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._contexts.clear()


registry = RunRegistry()
