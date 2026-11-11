"""流式输出实时过滤。

在模型 delta 发往前端之前处理高置信密钥与危险 HTML，支持跨 chunk 切割：

- 密钥：持有「正在形成的锚点/匹配」的尾部，完整后再交给 ``sanitize_output`` 打码；
- HTML：``<script>`` / ``<iframe>`` / ``<style>`` 整段丢弃，未闭合标签先扣留；
- 正常文本只扣留锚点前缀（≤ 11 字符）或未闭合标签，不影响流式体验。

落库前仍会跑一次完整 ``sanitize_output``，本模块是流式层的额外防线，不是唯一防线。
"""

from __future__ import annotations

from app.core.prompt_guard import sanitize_output

_MAX_TAG = 2 * 1024
_MAX_BLOCK = 16 * 1024
_MAX_SECRET = 256
_MAX_HOLD = 64 * 1024

# 锚点：(文本, 允许的 token 字符集, 最短长度)。Bearer 用点/下划线/连字符。
_ANCHORS: tuple[tuple[str, frozenset[str], int], ...] = (
    ("sk-", frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"), 16),
    ("AKIA", frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"), 16),
    ("ghp_", frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"), 20),
    (
        "Bearer ",
        frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"),
        20,
    ),
)
_PRIVATE_BEGIN = "-----BEGIN "
_BLOCK_TAGS: tuple[tuple[str, str], ...] = (
    ("script", "</script>"),
    ("iframe", "</iframe>"),
    ("style", "</style>"),
)


def _strip_blocks(buffer: str) -> tuple[str, int | None, set[str]]:
    """移除完整危险块；返回 (清理后文本, 未闭合块起点, 命中规则)。"""
    rules: set[str] = set()
    while True:
        lower = buffer.lower()
        found: tuple[int, str, str] | None = None
        for tag, close in _BLOCK_TAGS:
            index = lower.find(f"<{tag}")
            if index >= 0 and (found is None or index < found[0]):
                found = (index, tag, close)
        if found is None:
            return buffer, None, rules
        index, tag, close = found
        close_index = lower.find(close, index)
        if close_index < 0:
            if len(buffer) - index > _MAX_BLOCK:
                # 长时间未闭合：整段丢弃，避免无限缓存。
                rules.add(f"html_{tag}")
                return "", None, rules
            return buffer, index, rules
        buffer = buffer[:index] + buffer[close_index + len(close) :]
        rules.add(f"html_{tag}")


def _partial_secret_start(buffer: str) -> int:
    """返回必须扣留的起点；没有正在形成的密钥时返回 len(buffer)。"""
    hold = len(buffer)

    for anchor, allowed, _minimum in _ANCHORS:
        index = buffer.rfind(anchor)
        if index < 0:
            continue
        tail = buffer[index + len(anchor) :]
        # 空 tail（刚出现 "-" / " "）或全是 token 字符（尚无终止符）都要扣留；
        # 一旦出现非 token 字符，完整密钥已可交给 sanitize_output。
        if all(char in allowed for char in tail):
            hold = min(hold, index)

    begin = buffer.rfind(_PRIVATE_BEGIN)
    if begin >= 0 and "PRIVATE KEY-----" in buffer[begin : begin + 64]:
        if "-----END" not in buffer[begin:]:
            hold = min(hold, begin)

    # buffer 末尾是某个锚点的前缀（如 "s"、"Bear"）时也要扣留。
    for anchor, _allowed, _minimum in _ANCHORS:
        for length in range(min(len(anchor) - 1, len(buffer)), 0, -1):
            if buffer.endswith(anchor[:length]):
                hold = min(hold, len(buffer) - length)
                break
    for length in range(min(len(_PRIVATE_BEGIN) - 1, len(buffer)), 0, -1):
        if buffer.endswith(_PRIVATE_BEGIN[:length]):
            hold = min(hold, len(buffer) - length)
            break

    return hold


def _incomplete_tag_start(buffer: str) -> int | None:
    """最后一个未闭合 ``<`` 的位置；用于扣留可能含事件属性的标签。"""
    index = buffer.rfind("<")
    if index < 0:
        return None
    if ">" not in buffer[index:]:
        if len(buffer) - index <= _MAX_TAG:
            return index
        return None
    return None


class StreamingOutputFilter:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._buffer = ""
        self._rules: set[str] = set()

    @property
    def rules(self) -> set[str]:
        return set(self._rules)

    def feed(self, text: str) -> str:
        if not self.enabled:
            return text
        if not text:
            return ""
        self._buffer += text

        cleaned, block_start, block_rules = _strip_blocks(self._buffer)
        self._rules.update(block_rules)
        self._buffer = cleaned

        if block_start is not None:
            # 未闭合的危险块：从块起点扣留，块前的正常文本照常输出。
            hold = block_start
        else:
            hold = _partial_secret_start(self._buffer)
            tag_start = _incomplete_tag_start(self._buffer)
            if tag_start is not None:
                hold = min(hold, tag_start)

        if hold <= 0:
            if len(self._buffer) > _MAX_HOLD:
                # 异常长输入：避免无限缓存，整体打码后吐出。
                content, hits = sanitize_output(self._buffer)
                self._rules.update(hits)
                self._buffer = ""
                return content
            return ""

        prefix = self._buffer[:hold]
        self._buffer = self._buffer[hold:]
        content, hits = sanitize_output(prefix)
        self._rules.update(hits)
        return content

    def flush(self) -> str:
        if not self.enabled or not self._buffer:
            return ""
        content, hits = sanitize_output(self._buffer)
        self._rules.update(hits)
        self._buffer = ""
        return content


__all__ = ["StreamingOutputFilter"]
