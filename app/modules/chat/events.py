"""运行事件：序号、持久化、SSE 帧格式化。"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.chat import RunEvent
from app.modules.chat.bus import RunEventBus

logger = logging.getLogger(__name__)

# 结束事件：SSE 收到后可以关闭连接。
TERMINAL_EVENTS = {"message.done", "error"}


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def format_sse(seq: int, event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    # data 必须是单行；json.dumps 已转义换行。id 使用 run 内递增序号。
    return f"id: {seq}\nevent: {event}\ndata: {payload}\n\n"


class RunEventPublisher:
    """为一次 run 生成单调递增的 seq，并先落库再广播。"""

    def __init__(
        self,
        run_id: uuid.UUID,
        session_factory: async_sessionmaker[AsyncSession],
        event_bus: RunEventBus,
        *,
        start_seq: int = 0,
    ) -> None:
        self.run_id = run_id
        self._session_factory = session_factory
        self._bus = event_bus
        self.seq = start_seq

    async def emit(
        self,
        event: str,
        data: dict[str, Any],
        *,
        persist: bool = True,
        event_type: str = "message",
        status: str = "ok",
        title: str = "",
        subtitle: str = "",
        duration_ms: int | None = None,
        tokens: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.seq += 1
        ts = utc_now()
        body: dict[str, Any] = {
            "run_id": str(self.run_id),
            "ts": to_iso(ts),
            "seq": self.seq,
            **data,
        }
        if persist:
            payload = {**body, "_event": event}
            async with self._session_factory() as session:
                session.add(
                    RunEvent(
                        run_id=self.run_id,
                        seq=self.seq,
                        type=event_type,
                        status=status,
                        title=title[:200],
                        subtitle=subtitle[:200],
                        duration_ms=duration_ms,
                        tokens=tokens or {},
                        payload=payload,
                        error=error,
                        ts=ts,
                    )
                )
                await session.commit()
        await self._bus.publish(self.run_id, {"event": event, "data": body})
        return body
