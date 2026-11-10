"""知识库文本切分：把解析出的纯文本切成带章节标题元数据的片段。

本模块是纯函数层：不读写数据库、不访问网络、不依赖存储后端。

分层策略（spec §9.2）：

1. 先按 Markdown ATX 标题（``#``~``######``）分节，并记录嵌套标题路径；
2. 节内按空行分段；
3. 段落超过 ``size`` 时先按句末标点（``。！？.!?``）切分，单句仍超长再按长度硬切；
4. 同节内相邻段落尽量合并到 ``size``，跨节绝不合并；
5. 句切/硬切产生的相邻块之间保留 ``overlap`` 个字符的重叠。

``char_start`` 始终是块内容在原文中的起始偏移（重叠前缀也算在内容内），
因此可以直接用于引用定位。空白输入返回空列表，由解析层负责拒绝真正为空的文件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 句末标点与常见的收尾引号/括号：切句时把它们留在一句话里。
_SENTENCE_TERMINATORS = frozenset("。！？.!?")
_SENTENCE_CLOSERS = frozenset("”’」』）)】]")

# 文档尾部不足该长度的块并入前一块（spec §9.2）。
_MIN_TAIL_CHARS = 20

# Markdown ATX 标题：最多 3 个前导空格，1~6 个 #，后接空白或行尾。
_HEADING_RE = re.compile(r"^( {0,3})(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_CLOSING_HASHES_RE = re.compile(r"[ \t]+#+$")


@dataclass
class Chunk:
    """一个可入库的文本片段。

    :param content: 片段正文（不含 Markdown 标题行）
    :param index: 文档内 0 起的连续序号
    :param heading: 嵌套标题路径，多层用 `` > `` 连接；无标题时为空串
    :param char_start: 片段内容在原文中的起始偏移，用于引用定位
    :param metadata: 额外元数据，至少含 ``heading``；截断时末块含 ``truncated``
    """

    content: str
    index: int
    heading: str
    char_start: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Paragraph:
    """原文中的一个空行分隔段落。"""

    start: int
    text: str


@dataclass
class _Section:
    """一个 Markdown 标题下的内容；节与节之间绝不合并。"""

    heading: str
    paragraphs: list[_Paragraph] = field(default_factory=list)


@dataclass
class _Unit:
    """节内可打包的最小单元：普通段落，或超长段落的一个句切/硬切片。"""

    text: str
    start: int
    starts_paragraph: bool
    continuation: bool


@dataclass
class _Draft:
    """切分中间结果，合并尾部小块时需要就地改写正文。"""

    content: str
    heading: str
    char_start: int
    section: int


def split_text(text: str, *, size: int, overlap: int, max_chunks: int) -> list[Chunk]:
    """按 Markdown 标题与段落把 ``text`` 切成块。

    :param text: 解析层输出的纯文本；空白输入返回空列表
    :param size: 单块目标字符数（含 overlap 前缀）上限，必须为正
    :param overlap: 句切/硬切相邻块的重叠字符数，必须满足 ``0 <= overlap < size``
    :param max_chunks: 单文档最大块数；超出截断并给最后保留的块打 ``truncated``
    :raises ValueError: 参数非法
    """
    if not text or not text.strip():
        return []
    _validate_args(size=size, overlap=overlap, max_chunks=max_chunks)
    if max_chunks == 0:
        return []

    drafts: list[_Draft] = []
    for section_index, section in enumerate(_parse_sections(text)):
        drafts.extend(
            _split_section(
                section,
                section_index=section_index,
                size=size,
                overlap=overlap,
            )
        )

    _merge_short_tail(drafts)
    truncated = len(drafts) > max_chunks
    if truncated:
        drafts = drafts[:max_chunks]

    chunks: list[Chunk] = []
    for index, draft in enumerate(drafts):
        metadata: dict[str, Any] = {"heading": draft.heading}
        if truncated and index == len(drafts) - 1:
            metadata["truncated"] = True
        chunks.append(
            Chunk(
                content=draft.content,
                index=index,
                heading=draft.heading,
                char_start=draft.char_start,
                metadata=metadata,
            )
        )
    return chunks


def _validate_args(*, size: int, overlap: int, max_chunks: int) -> None:
    """参数不合法时立即报错，避免后续出现死循环或越界切片。"""
    if size <= 0:
        raise ValueError("切分块大小 size 必须为正整数")
    if overlap < 0:
        raise ValueError("切分重叠 overlap 不能为负数")
    if overlap >= size:
        raise ValueError("切分重叠 overlap 必须小于块大小 size")
    if max_chunks < 0:
        raise ValueError("单文档块数上限 max_chunks 不能为负数")


# ---------------------------------------------------------------- Markdown 分节
def _parse_sections(text: str) -> list[_Section]:
    """扫描原文，按 ATX 标题切节、按空行切段，并记录段落在原文中的偏移。"""
    sections = [_Section(heading="")]
    heading_stack: list[tuple[int, str]] = []
    paragraph_start: int | None = None
    paragraph_end = 0

    def flush_paragraph() -> None:
        nonlocal paragraph_start
        if paragraph_start is None:
            return
        # 直接切原文而不是重新拼接，保证 char_start 对 CRLF 等换行也精确。
        raw = text[paragraph_start:paragraph_end]
        leading = len(raw) - len(raw.lstrip())
        content = raw.strip()
        if content:
            sections[-1].paragraphs.append(
                _Paragraph(start=paragraph_start + leading, text=content)
            )
        paragraph_start = None

    offset = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        heading = _match_heading(line)
        if heading is not None:
            flush_paragraph()
            level, title = heading
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            sections.append(_Section(heading=" > ".join(item for _, item in heading_stack)))
        elif not line.strip():
            flush_paragraph()
        else:
            if paragraph_start is None:
                paragraph_start = offset
            paragraph_end = offset + len(line)
        offset += len(raw_line)

    flush_paragraph()
    return sections


def _match_heading(line: str) -> tuple[int, str] | None:
    """识别一行是否为 ATX 标题，返回 (层级, 标题文本)。"""
    match = _HEADING_RE.match(line)
    if match is None:
        return None
    level = len(match.group(2))
    title = (match.group(3) or "").strip()
    # 去掉 ATX 标题可选的收尾 #，如「## 退货政策 ##」。
    title = _CLOSING_HASHES_RE.sub("", title).strip()
    return level, title


# -------------------------------------------------------------------- 分节切块
def _split_section(
    section: _Section,
    *,
    section_index: int,
    size: int,
    overlap: int,
) -> list[_Draft]:
    """切分单节：把普通段落与超长段落的切片按原文顺序打包成块。"""
    drafts: list[_Draft] = []
    buffer: list[_Unit] = []
    buffer_len = 0
    buffer_limit = size

    def flush() -> None:
        nonlocal buffer, buffer_len
        if not buffer:
            return
        body = buffer[0].text
        for unit in buffer[1:]:
            body += ("\n\n" if unit.starts_paragraph else "") + unit.text
        prefix = ""
        if buffer[0].continuation and drafts and overlap > 0:
            # 句切/硬切产生的相邻块，从前一块末尾取 overlap 个字符作为前缀。
            prefix = drafts[-1].content[-overlap:]
        drafts.append(
            _Draft(
                content=prefix + body,
                heading=section.heading,
                char_start=max(0, buffer[0].start - len(prefix)),
                section=section_index,
            )
        )
        buffer = []
        buffer_len = 0

    for paragraph in section.paragraphs:
        if len(paragraph.text) > size:
            units = _split_long_paragraph_units(paragraph, size=size, overlap=overlap)
        else:
            units = [
                _Unit(
                    text=paragraph.text,
                    start=paragraph.start,
                    starts_paragraph=True,
                    continuation=False,
                )
            ]
        for unit in units:
            separator = 2 if buffer and unit.starts_paragraph else 0
            candidate = buffer_len + separator + len(unit.text)
            if buffer and candidate > buffer_limit:
                flush()
            if not buffer:
                # 以超长段落的续片开头时，要给 overlap 前缀预留位置。
                if unit.continuation and drafts and overlap > 0:
                    buffer_limit = max(1, size - overlap)
                else:
                    buffer_limit = size
                candidate = len(unit.text)
            buffer.append(unit)
            buffer_len = candidate
            if buffer_len >= buffer_limit:
                flush()

    flush()
    return drafts


def _split_long_paragraph_units(
    paragraph: _Paragraph,
    *,
    size: int,
    overlap: int,
) -> list[_Unit]:
    """把超长段落拆成句切/硬切后的连续单元，供节内统一打包。"""
    # 除首块外都要给 overlap 前缀留位置；按这个预算硬切，块长度更均匀。
    piece_limit = max(1, size - overlap)
    units: list[_Unit] = []
    for span in _sentence_spans(paragraph.text):
        for piece_start, piece_end in _hard_split_span(span, piece_limit):
            units.append(
                _Unit(
                    text=paragraph.text[piece_start:piece_end],
                    start=paragraph.start + piece_start,
                    starts_paragraph=not units,
                    continuation=bool(units),
                )
            )
    return units


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """按句末标点把文本切成连续区间，标点后的收尾引号/括号跟随前一句。"""
    spans: list[tuple[int, int]] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        if text[index] not in _SENTENCE_TERMINATORS:
            index += 1
            continue
        end = index + 1
        while end < length and text[end] in _SENTENCE_TERMINATORS:
            end += 1
        while end < length and text[end] in _SENTENCE_CLOSERS:
            end += 1
        spans.append((start, end))
        start = end
        index = end
    if start < length:
        spans.append((start, length))
    return spans


def _hard_split_span(span: tuple[int, int], limit: int) -> list[tuple[int, int]]:
    """把区间按 ``limit`` 均分成若干段，避免出现 1~2 字符的硬切碎片。"""
    start, end = span
    length = end - start
    if length <= limit:
        return [span]
    parts = (length + limit - 1) // limit
    step = (length + parts - 1) // parts
    return [(position, min(position + step, end)) for position in range(start, end, step)]


def _merge_short_tail(drafts: list[_Draft]) -> None:
    """文档尾部不足 20 字符的块并入前一块；跨节不合并。"""
    if len(drafts) < 2:
        return
    last = drafts[-1]
    previous = drafts[-2]
    if last.section != previous.section or len(last.content) >= _MIN_TAIL_CHARS:
        return
    previous.content = f"{previous.content}\n\n{last.content}"
    drafts.pop()
