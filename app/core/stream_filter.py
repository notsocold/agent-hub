"""流式输出实时过滤。

在模型 delta 发往前端之前处理高置信密钥与危险 HTML，支持跨 chunk 切割：

- 密钥：持有「正在形成的锚点/匹配」的尾部，完整后再交给 ``sanitize_output`` 打码；
- PEM 私钥：从 ``-----BEGIN`` 起扣留，直到完整 ``-----END ... PRIVATE KEY-----``；
- HTML：``<script>`` / ``<iframe>`` / ``<style>`` 整段丢弃，未闭合块在 ``flush()`` 也丢弃；
- 正常文本只扣留锚点前缀/正在形成的 token 或未闭合标签，不影响流式体验。

落库前仍会跑一次完整 ``sanitize_output``，本模块是流式层的额外防线，不是唯一防线。
"""

from __future__ import annotations

import re

from app.core.prompt_guard import sanitize_output

_MAX_TAG = 2 * 1024
_MAX_BLOCK = 16 * 1024
_MAX_HOLD = 64 * 1024

# 普通 token 锚点：(文本, 允许的 token 字符集)。Bearer 有独立的空白状态机。
_ANCHORS: tuple[tuple[str, frozenset[str]], ...] = (
    ("sk-", frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")),
    ("AKIA", frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")),
    ("ghp_", frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")),
)
_BEARER = "Bearer"
_BEARER_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
_PRIVATE_BEGIN = "-----BEGIN "
_PRIVATE_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
)
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
        rules.add(f"html_{tag}")
        close_index = lower.find(close, index)
        if close_index < 0:
            if len(buffer) - index > _MAX_BLOCK:
                # 异常长的未闭合块：丢弃块本身，保留块前文本。
                return buffer[:index], None, rules
            return buffer, index, rules
        buffer = buffer[:index] + buffer[close_index + len(close) :]


def _private_key_hold(buffer: str) -> int:
    """返回 PEM 私钥必须扣留的起点；没有未完成私钥时返回 len(buffer)。"""
    hold = len(buffer)
    last_begin = buffer.rfind(_PRIVATE_BEGIN)
    if last_begin >= 0 and _PRIVATE_BLOCK.match(buffer, last_begin) is None:
        hold = min(hold, last_begin)

    # 末尾是 BEGIN 前缀（例如 "-----BEG"）：只有它不属于已完整私钥时才扣留。
    # 必须看最后一个完整块，否则「两块 PEM、最后一块以 END 结尾」时会把
    # END 的末 5 个破折号误当成新 BEGIN 的前缀，导致最后一块漏打码。
    complete_blocks = list(_PRIVATE_BLOCK.finditer(buffer))
    last_complete_end = complete_blocks[-1].end() if complete_blocks else -1
    for length in range(min(len(_PRIVATE_BEGIN) - 1, len(buffer)), 0, -1):
        if not buffer.endswith(_PRIVATE_BEGIN[:length]):
            continue
        start = len(buffer) - length
        if complete_blocks and start < last_complete_end:
            break
        hold = min(hold, start)
        break
    return hold


def _partial_secret_start(buffer: str) -> int:
    """返回必须扣留的起点；没有正在形成的密钥时返回 len(buffer)。"""
    hold = _private_key_hold(buffer)

    for anchor, allowed in _ANCHORS:
        index = buffer.rfind(anchor)
        if index < 0:
            continue
        tail = buffer[index + len(anchor) :]
        if all(char in allowed for char in tail):
            hold = min(hold, index)

    # Bearer 后允许任意空白（含换行/制表符/多空格），再到 token。
    bearer = buffer.rfind(_BEARER)
    if bearer >= 0:
        position = bearer + len(_BEARER)
        while position < len(buffer) and buffer[position].isspace():
            position += 1
        tail = buffer[position:]
        if all(char in _BEARER_ALLOWED for char in tail):
            hold = min(hold, bearer)

    # buffer 末尾是某个锚点的前缀（如 "s"、"Bear"）时也要扣留。
    for anchor, _allowed in _ANCHORS:
        for length in range(min(len(anchor) - 1, len(buffer)), 0, -1):
            if buffer.endswith(anchor[:length]):
                hold = min(hold, len(buffer) - length)
                break
    for length in range(min(len(_BEARER) - 1, len(buffer)), 0, -1):
        if buffer.endswith(_BEARER[:length]):
            hold = min(hold, len(buffer) - length)
            break
    return hold


def _incomplete_tag_start(buffer: str) -> int | None:
    """最后一个未闭合 ``<`` 的位置；用于扣留可能含事件属性的标签。"""
    index = buffer.rfind("<")
    if index < 0:
        return None
    if ">" in buffer[index:]:
        return None
    # 只把「看起来像标签开头」的 < 当作未闭合标签；"5 < 10" 这类正常文本不扣留。
    next_char = buffer[index + 1] if index + 1 < len(buffer) else ""
    if next_char and not (next_char.isalpha() or next_char in "/!?"):
        return None
    if len(buffer) - index <= _MAX_TAG:
        return index
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
        cleaned, block_start, block_rules = _strip_blocks(self._buffer)
        self._rules.update(block_rules)
        if block_start is not None:
            # 流结束仍未闭合的危险块：整段丢弃（含块后内容）。
            cleaned = cleaned[:block_start]

        last_begin = cleaned.rfind(_PRIVATE_BEGIN)
        if last_begin >= 0 and _PRIVATE_BLOCK.match(cleaned, last_begin) is None:
            # 未完成的私钥不能交给通用正则（它会匹配不到而泄漏）；截断并打码。
            cleaned = f"{cleaned[:last_begin]}***"
            self._rules.add("private_key")

        tag_start = _incomplete_tag_start(cleaned)
        if tag_start is not None:
            cleaned = cleaned[:tag_start]
            self._rules.add("html_unclosed")

        content, hits = sanitize_output(cleaned)
        self._rules.update(hits)
        self._buffer = ""
        return content


__all__ = ["StreamingOutputFilter"]
