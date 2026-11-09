"""进程内运行事件总线。

SSE 连接与图执行解耦：客户端断开不影响运行，重连时先回放数据库事件再订阅实时事件。
单实例够用；多实例需要 Redis Stream 等共享总线（M6）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from typing import Any


class RunEventBus:
    def __init__(self) -> None:
        self._channels: dict[uuid.UUID, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, run_id: uuid.UUID) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2048)
        async with self._lock:
            self._channels[run_id].add(queue)
        return queue

    async def unsubscribe(self, run_id: uuid.UUID, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            channel = self._channels.get(run_id)
            if channel is not None:
                channel.discard(queue)
                if not channel:
                    self._channels.pop(run_id, None)

    async def publish(self, run_id: uuid.UUID, event: dict[str, Any]) -> None:
        async with self._lock:
            subscribers = list(self._channels.get(run_id, ()))
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 慢客户端不能阻塞图执行；丢弃实时帧，重连时靠回放补齐。
                continue


bus = RunEventBus()
