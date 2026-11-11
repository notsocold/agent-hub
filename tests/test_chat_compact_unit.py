"""M5 遗留截断类回归单测：审批 payload_preview 与工具参数脱敏。"""

from __future__ import annotations

import json

from app.modules.chat.approvals import _compact
from app.modules.chat.tools import ToolRunner


def test_compact_never_exceeds_limit() -> None:
    """`approvals.payload_preview` 列为 String(500)，截断后绝不能是 501。"""
    preview = _compact({"text": "x" * 1000}, 500)
    assert len(preview) == 500
    assert preview.endswith("…")

    short = _compact({"text": "ok"}, 500)
    assert short == json.dumps({"text": "ok"}, ensure_ascii=False)
    assert not short.endswith("…")


def test_sanitize_args_degrades_safely_when_truncated() -> None:
    """>4000 字符 JSON 截断后无法解析时，安全降级而不是抛 JSONDecodeError。"""
    args = {"text": "x" * 10_000}
    result = ToolRunner._sanitize_args(args)
    assert isinstance(result, dict)
    assert "_truncated" in result
    assert isinstance(result["_truncated"], str)
    # 降级占位仍受 limit 约束，且整体可 JSON 序列化（JSONB 落库要求）。
    assert len(result["_truncated"]) <= 4000
    json.dumps(result)


def test_sanitize_args_keeps_small_args() -> None:
    assert ToolRunner._sanitize_args({"agent": "fake", "task": "x"}) == {
        "agent": "fake",
        "task": "x",
    }
