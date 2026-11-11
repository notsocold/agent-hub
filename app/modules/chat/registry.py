"""运行任务注册表：取消、审批唤醒与进程内生命周期管理。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from app.core.metrics import record_active_run
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
    # 本轮已产出的引用（CitationOut 形状的 dict）；与 ToolRunner.citations 是同一对象，
    # 由工具追加、由终态统一去重编号后随 message.done 发出并落库。
    citations: list[dict] = field(default_factory=list)
    # 审批等待已迁移到 LangGraph interrupt()（M5）：进程内不再保存审批 waiter。
    # A2A HITL：input_required/auth_required 期间挂起；tasks 动作接口写入决定并唤醒。
    # decision 形如 {"action": "resume"|"authorize"|"reject"|"cancel"|"expired", ...}。
    a2a_task_waiters: dict[uuid.UUID, asyncio.Event] = field(default_factory=dict)
    a2a_task_decisions: dict[uuid.UUID, dict] = field(default_factory=dict)

    def wait_for_a2a_task(self, task_id: uuid.UUID) -> asyncio.Event:
        """A2A 任务等待注册：在事件发出前注册，消除决定先到的竞态。"""
        event = asyncio.Event()
        self.a2a_task_waiters[task_id] = event
        return event

    def take_a2a_decision(self, task_id: uuid.UUID) -> dict | None:
        return self.a2a_task_decisions.pop(task_id, None)

    def resolve_a2a_task(self, task_id: uuid.UUID, decision: dict) -> None:
        """tasks 动作接口唤醒：写入决定并通知等待中的工具执行。"""
        self.a2a_task_decisions[task_id] = decision
        waiter = self.a2a_task_waiters.get(task_id)
        if waiter is not None:
            waiter.set()


class RunRegistry:
    def __init__(self) -> None:
        self._contexts: dict[uuid.UUID, RunContext] = {}

    def register(self, context: RunContext) -> None:
        self._contexts[context.run_id] = context

    def start(self, context: RunContext, coroutine) -> None:
        context.task = asyncio.create_task(coroutine, name=f"run-{context.run_id}")
        self._contexts[context.run_id] = context
        record_active_run(1)

        def _cleanup(_: asyncio.Task) -> None:
            record_active_run(-1)
            # 同一 run 可能先有首轮任务、后有恢复任务：只清理自己，避免误删新 context。
            if self._contexts.get(context.run_id) is context:
                self._contexts.pop(context.run_id, None)

        context.task.add_done_callback(_cleanup)

    def get(self, run_id: uuid.UUID) -> RunContext | None:
        return self._contexts.get(run_id)

    def is_running(self, run_id: uuid.UUID) -> bool:
        context = self._contexts.get(run_id)
        return bool(context and context.task and not context.task.done())

    async def drain(self, timeout: float) -> bool:
        """等待所有已注册 run 任务结束；超时返回 False，不取消任务。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while True:
            tasks = [
                context.task
                for context in list(self._contexts.values())
                if context.task is not None and not context.task.done()
            ]
            if not tasks:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.wait(tasks, timeout=min(remaining, 1.0))

    def cancel(self, run_id: uuid.UUID) -> bool:
        context = self._contexts.get(run_id)
        if context is None or context.task is None or context.task.done():
            return False
        context.cancel_requested = True
        # A2A 等待同样唤醒：工具收到 cancel 决策后返回已取消的 ToolMessage。
        for task_id, waiter in context.a2a_task_waiters.items():
            context.a2a_task_decisions.setdefault(task_id, {"action": "cancel"})
            waiter.set()
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
