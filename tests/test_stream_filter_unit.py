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


PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA1234567890abcdef\n"
    "-----END RSA PRIVATE KEY-----"
)


@pytest.mark.parametrize("cut", range(1, len(PEM)))
def test_pem_split_never_leaks(cut: int) -> None:
    output_filter = StreamingOutputFilter()
    out = output_filter.feed(PEM[:cut]) + output_filter.feed(PEM[cut:]) + output_filter.flush()
    assert "MIIEowIBAAKCAQEA1234567890abcdef" not in out
    assert "BEGIN RSA PRIVATE KEY" not in out


def test_pem_at_stream_end_never_leaks() -> None:
    output_filter = StreamingOutputFilter()
    out = output_filter.feed(PEM) + output_filter.flush()
    assert "BEGIN RSA PRIVATE KEY" not in out
    assert "MIIEowIBAAKCAQEA1234567890abcdef" not in out


@pytest.mark.parametrize("separator", ["\n", "\t", "  "])
def test_bearer_whitespace_split_never_leaks(separator: str) -> None:
    token = "abcdefghijklmnopqrstuvwxyz012345"
    raw = f"Authorization: Bearer{separator}{token}"
    output_filter = StreamingOutputFilter()
    out = ""
    for index in range(0, len(raw), 2):
        out += output_filter.feed(raw[index : index + 2])
    out += output_filter.flush()
    assert token not in out


@pytest.mark.parametrize("tag", ["script", "iframe", "style"])
def test_unclosed_html_block_dropped_on_flush(tag: str) -> None:
    output_filter = StreamingOutputFilter()
    out = output_filter.feed(f"hello <{tag}>alert(1)") + output_filter.flush()
    assert f"<{tag}>" not in out
    assert "alert(1)" not in out
