"""日志配置与请求上下文。

M6 起支持 ``LOG_FORMAT=json``：每行一个 JSON，字段含 request_id、run_id、
conversation_id 与当前 OTel trace/span id；``text`` 模式保持 M1–M5 的可读格式。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import format_span_id, format_trace_id

from app import __version__
from app.core.config import Settings
from app.core.errors import request_id_ctx
from app.core.log_context import conversation_id_ctx, run_id_ctx

# 允许写入 JSON 的 logging extra 键；其余键一律丢弃，避免把任意对象/密钥写进日志。
_EXTRA_KEYS = frozenset(
    {"event", "tool", "a2a_task_id", "tool_call_id", "status", "duration_ms"}
)


class RequestIdFilter(logging.Filter):
    """把当前请求 ID 注入日志记录，便于串联一次请求。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_ctx.get() or "-"
        return True


def _current_trace_ids() -> tuple[str, str]:
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return "-", "-"
    return format_trace_id(span_context.trace_id), format_span_id(span_context.span_id)


class JsonFormatter(logging.Formatter):
    """一行一个 JSON 的日志格式。"""

    def __init__(self, *, service: str, env: str, version: str) -> None:
        super().__init__()
        self._service = service
        self._env = env
        self._version = version

    def format(self, record: logging.LogRecord) -> str:
        trace_id, span_id = _current_trace_ids()
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self._service,
            "env": self._env,
            "version": self._version,
            "request_id": getattr(record, "request_id", "-"),
            "run_id": run_id_ctx.get() or "-",
            "conversation_id": conversation_id_ctx.get() or "-",
            "trace_id": trace_id,
            "span_id": span_id,
        }
        for key in _EXTRA_KEYS:
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


def configure_logging(settings: Settings, *, force: bool = False) -> None:
    """按配置初始化根 logger；已在运行的应用重复调用时只更新级别。"""
    root = logging.getLogger()
    level = logging.getLevelName(settings.log_level)
    if root.handlers and not force:
        root.setLevel(level)
        return

    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    if settings.log_format == "json":
        handler.setFormatter(
            JsonFormatter(service=settings.app_name, env=settings.env, version=__version__)
        )
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
    handler.addFilter(RequestIdFilter())
    root.addHandler(handler)
    root.setLevel(level)


def reset_logging() -> None:
    """测试辅助：清空根 logger 的处理器。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.WARNING)


__all__ = ["JsonFormatter", "RequestIdFilter", "configure_logging", "reset_logging"]
