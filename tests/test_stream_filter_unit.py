"""流式输出过滤器：密钥跨 chunk 不泄漏，危险 HTML 整段丢弃。"""

from __future__ import annotations

import pytest

from app.core.stream_filter import StreamingOutputFilter


@pytest.mark.parametrize("cut", range(1, 30))
def test_openai_key_split_never_leaks(cut: int) -> None:
    raw = "前缀 sk-abcdefghijklmnopqrstuvwxyz 后缀"
    output_filter = StreamingOutputFilter()
    out = output_filter.feed(raw[:cut]) + output_filter.feed(raw[cut:]) + output_filter.flush()
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in out
    assert "***" in out


def test_bearer_token_split_never_leaks() -> None:
    raw = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345"
    output_filter = StreamingOutputFilter()
    out = ""
    for index in range(0, len(raw), 3):
        out += output_filter.feed(raw[index : index + 3])
    out += output_filter.flush()
    assert "abcdefghijklmnopqrstuvwxyz012345" not in out
    assert "Bearer ***" in out


def test_normal_text_not_delayed() -> None:
    output_filter = StreamingOutputFilter()
    assert output_filter.feed("你好，世界") == "你好，世界"
    assert output_filter.flush() == ""


def test_script_block_dropped_across_chunks() -> None:
    output_filter = StreamingOutputFilter()
    out = (
        output_filter.feed("<scr")
        + output_filter.feed("ipt>alert(1)</script>正文")
        + output_filter.flush()
    )
    assert "<script" not in out.lower()
    assert "alert(1)" not in out
    assert "正文" in out


def test_disabled_passthrough() -> None:
    output_filter = StreamingOutputFilter(enabled=False)
    assert output_filter.feed("sk-abcdefghijklmnopqrstuvwxyz") == "sk-abcdefghijklmnopqrstuvwxyz"
    assert output_filter.flush() == ""
