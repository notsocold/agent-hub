"""RunRegistry 排水。"""

from __future__ import annotations

import asyncio
import uuid

from app.modules.chat.registry import RunContext, RunRegistry


def _context() -> RunContext:
    return RunContext(run_id=uuid.uuid4(), publisher=None)  # type: ignore[arg-type]


async def test_drain_waits_for_tasks() -> None:
    registry = RunRegistry()
    context = _context()
    registry.start(context, asyncio.sleep(0.2))
    assert await registry.drain(1.0) is True


async def test_drain_timeout_returns_false_and_shutdown_cancels() -> None:
    registry = RunRegistry()
    context = _context()
    registry.start(context, asyncio.sleep(5))
    assert await registry.drain(0.2) is False
    await registry.shutdown()
    assert context.task is not None and context.task.cancelled()


async def test_drain_with_no_tasks_is_true() -> None:
    assert await RunRegistry().drain(1.0) is True
