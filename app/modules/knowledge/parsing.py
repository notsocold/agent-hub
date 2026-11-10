"""知识库文档解析：把上传的原始字节转成纯文本、标题与格式相关元数据。

支持 ``.md`` / ``.markdown`` / ``.txt`` / ``.pdf`` / ``.csv`` / ``.json``。
本模块是纯函数层：不读写数据库、不访问网络、不依赖存储后端，便于单元测试。

约定：

- 扩展名统一小写后匹配，且先于任何内容嗅探，避免伪装后缀绕过限制；
- 解码失败、PDF 无文本层、内容为空等一律抛中文 :class:`ValueError`
  （:class:`UnsupportedDocumentError` 是其后缀不支持的子类），由入库层记录失败原因；
- 绝不静默返回乱码或空文档。
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PyPdfError

# 顺序同时用于错误提示中的支持列表。
_SUPPORTED_SUFFIXES = (".md", ".markdown", ".txt", ".pdf", ".csv", ".json")


class UnsupportedDocumentError(ValueError):
    """文件扩展名不受支持。"""


@dataclass(frozen=True)
class ParsedDocument:
    """解析结果。

    :param text: 提取出的纯文本（markdown 保留原始标记，不做渲染）
    :param title: 默认标题，取文件名去掉扩展名
    :param metadata: 格式相关元数据，PDF 含 ``pages``，CSV 含 ``rows``，其余为空
    """

    text: str
    title: str
    metadata: dict[str, Any]


def parse_document(filename: str, raw: bytes, *, max_pdf_pages: int) -> ParsedDocument:
    """按扩展名解析文档字节。

    :param filename: 上传时的原始文件名，只用于取扩展名和默认标题
    :param raw: 原始文件字节
    :param max_pdf_pages: PDF 页数上限，超出即拒绝（防解压/页数炸弹）
    :raises UnsupportedDocumentError: 扩展名不在支持列表内
    :raises ValueError: 解码失败、文件损坏、内容为空、PDF 无文本层或页数超限
    """
    path = Path(filename)
    suffix = path.suffix.lower()
    supported = "、".join(_SUPPORTED_SUFFIXES)
    if suffix not in _SUPPORTED_SUFFIXES:
        raise UnsupportedDocumentError(
            f"不支持的文件类型：{suffix or '（无扩展名）'}；仅支持 {supported}"
        )

    if suffix in {".md", ".markdown"}:
        text = _decode_utf8(raw, kind="Markdown")
        metadata: dict[str, Any] = {}
    elif suffix == ".txt":
        text = _decode_text(raw)
        metadata = {}
    elif suffix == ".csv":
        text, rows = _parse_csv(raw)
        metadata = {"rows": rows}
    elif suffix == ".json":
        text = _parse_json(raw)
        metadata = {}
    else:  # .pdf
        text, pages = _parse_pdf(raw, max_pdf_pages)
        metadata = {"pages": pages}

    if not text.strip():
        raise ValueError("文件内容为空（只有空白字符），无法解析")
    return ParsedDocument(text=text, title=path.stem, metadata=metadata)


def _decode_utf8(raw: bytes, *, kind: str) -> str:
    """严格按 UTF-8 解码；失败必须报错，不做猜测。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{kind} 文件不是合法的 UTF-8 编码") from exc


def _decode_text(raw: bytes) -> str:
    """文本解码：优先 UTF-8，失败回退 GB18030（兼容中文 Windows 导出）。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gb18030")
    except UnicodeDecodeError as exc:
        raise ValueError("文本文件编码无法识别：既不是 UTF-8 也不是 GB18030") from exc


def _parse_csv(raw: bytes) -> tuple[str, int]:
    """按 ``列名: 值`` 展开 CSV 每行，行内字段用 ``; `` 连接，行间用换行。"""
    text = _decode_text(raw)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    lines: list[str] = []
    for row in reader:
        fields: list[str] = []
        for column, value in row.items():
            if column is None:
                # 行宽超过表头时 csv 把多余单元格挂在 None 键下；
                # 保留为「额外列」而不是静默丢数据。
                extra = ", ".join(str(item) for item in value) if value else ""
                fields.append(f"额外列: {extra}")
                continue
            fields.append(f"{column}: {'' if value is None else value}")
        if fields:
            lines.append("; ".join(fields))
    return "\n".join(lines), len(lines)


def _parse_json(raw: bytes) -> str:
    """把 JSON 递归展平为 ``路径: 值`` 行，如 ``a.b: 1``、``tags[0]: 吊牌``。"""
    text = _decode_utf8(raw, kind="JSON")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("JSON 文件格式错误，无法解析") from exc
    lines: list[str] = []
    _flatten_json(payload, "", lines)
    return "\n".join(lines)


def _flatten_json(value: Any, path: str, lines: list[str]) -> None:
    """深度优先展平：对象键用 ``.``、数组下标用 ``[i]`` 连接。"""
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            _flatten_json(item, child, lines)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            child = f"{path}[{index}]" if path else f"[{index}]"
            _flatten_json(item, child, lines)
        return
    # 顶层就是标量时 path 为空，用 ``$`` 占位，避免输出以冒号开头的怪行。
    lines.append(f"{path or '$'}: {_json_scalar(value)}")


def _json_scalar(value: Any) -> str:
    """字符串保留原样（中文可读），数字/布尔/null 用 JSON 字面量。"""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _parse_pdf(raw: bytes, max_pdf_pages: int) -> tuple[str, int]:
    """逐页提取 PDF 文本层，返回 (文本, 页数)。"""
    try:
        reader = PdfReader(io.BytesIO(raw))
        pages = reader.pages
        page_count = len(pages)
    except PyPdfError as exc:
        raise ValueError("PDF 文件解析失败：文件已损坏或不是有效的 PDF") from exc

    if page_count > max_pdf_pages:
        raise ValueError(f"PDF 页数 {page_count} 超过上限 {max_pdf_pages}")

    try:
        page_texts = [page.extract_text() or "" for page in pages]
    except PyPdfError as exc:
        raise ValueError("PDF 文本提取失败：文件已损坏或已加密") from exc

    text = "\n".join(page_texts)
    if not text.strip():
        raise ValueError("PDF 未提取到文本（可能是扫描件）")
    return text, page_count
