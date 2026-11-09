"""不透明游标编解码。

游标只承载排序键，前端不得解析；编码使用 urlsafe base64，避免 URL 转义问题。

调用方直接把 ORM 字段放进 payload（``last.id`` 是 ``UUID``、``created_at`` 是
``datetime``）。``json.dumps`` 不认识这些类型，历史上会抛 ``TypeError`` 并被
全局异常处理成 500——只要列表需要返回 ``next_cursor`` 就会触发。因此编码层统一
做 JSON-safe 归一化：``UUID`` 转标准字符串、``datetime`` 转 ISO 8601，解码后由
各业务的游标判断再用 ``uuid.UUID(...)`` / ``datetime.fromisoformat(...)`` 还原。
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import date, datetime
from typing import Any

from app.core.errors import ValidationFailedError


def _json_safe(value: Any) -> str:
    """把游标值转成确定的字符串表示，避免依赖隐式 ``str()``。"""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"游标值不可序列化：{type(value).__name__}")


def encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_safe,
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> dict[str, Any]:
    if not cursor:
        return {}
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, TypeError) as exc:
        raise ValidationFailedError("分页游标无效") from exc
    if not isinstance(data, dict):
        raise ValidationFailedError("分页游标无效")
    return data
