"""不透明游标编解码。

游标只承载排序键，前端不得解析；编码使用 urlsafe base64，避免 URL 转义问题。
"""

from __future__ import annotations

import base64
import json
from typing import Any

from app.core.errors import ValidationFailedError


def encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
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
