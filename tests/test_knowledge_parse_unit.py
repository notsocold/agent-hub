"""知识库文档解析单元测试。

不依赖数据库、不访问网络：PDF 样本由 ``pypdf.PdfWriter`` 在内存中现场生成，
不提交二进制 fixture。pypdf 只能写页面结构、不能绘制文字，因此 PDF 样本仅使用
ASCII 文本；中文路径由 ``.md`` / ``.txt`` / ``.csv`` / ``.json`` 用例覆盖。
"""

from __future__ import annotations

import io
import json
from dataclasses import FrozenInstanceError

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.modules.knowledge.parsing import (
    ParsedDocument,
    UnsupportedDocumentError,
    parse_document,
)


# ---------------------------------------------------------------------- PDF 样本
def _make_pdf(*pages: str) -> bytes:
    """生成含一到多页的 PDF；参数为每页文本，空字符串表示该页没有文本层。

    页面内容流手动写入 ``BT /F1 12 Tf ... Tj ET``，并挂上 Helvetica 字体资源。
    文本必须是 ASCII（pypdf 不绘制文字，也无法嵌入中文字体）。
    """
    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        if not text:
            continue
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# ------------------------------------------------------------------- 通用与 markdown
def test_markdown_passthrough():
    doc = parse_document("a.md", "# 标题\n\n正文".encode(), max_pdf_pages=10)
    assert "标题" in doc.text and "正文" in doc.text
    assert doc.title == "a"
    assert doc.metadata == {}


def test_markdown_extension_alias():
    doc = parse_document("a.markdown", "正文".encode(), max_pdf_pages=10)
    assert doc.text == "正文" and doc.title == "a"


def test_uppercase_extension_ok():
    assert parse_document("A.MD", "正文".encode(), max_pdf_pages=10).text


def test_title_uses_filename_without_extension():
    doc = parse_document("退货政策.v2.md", "正文".encode(), max_pdf_pages=10)
    assert doc.title == "退货政策.v2"


def test_markdown_invalid_utf8_rejected():
    with pytest.raises(ValueError, match="UTF-8"):
        parse_document("a.md", b"\xff\xfe\x00", max_pdf_pages=10)


def test_unsupported_extension():
    with pytest.raises(UnsupportedDocumentError, match="exe"):
        parse_document("a.exe", b"x", max_pdf_pages=10)


def test_unsupported_extension_checked_before_content():
    # 扩展名优先于内容嗅探：内容像 markdown 也不能绕过扩展名检查。
    with pytest.raises(UnsupportedDocumentError):
        parse_document("a.exe", "# 标题".encode(), max_pdf_pages=10)


def test_unsupported_document_error_is_value_error():
    assert issubclass(UnsupportedDocumentError, ValueError)


def test_empty_file_rejected():
    with pytest.raises(ValueError, match="为空"):
        parse_document("a.txt", b"", max_pdf_pages=10)


def test_whitespace_only_file_rejected():
    with pytest.raises(ValueError, match="为空"):
        parse_document("a.md", b"  \n\t ", max_pdf_pages=10)


# -------------------------------------------------------------------------- txt
def test_txt_utf8():
    doc = parse_document("note.txt", "退货需保留吊牌".encode(), max_pdf_pages=10)
    assert "吊牌" in doc.text and doc.title == "note"


def test_txt_gb18030_fallback():
    doc = parse_document("note.txt", "退货需保留吊牌".encode("gb18030"), max_pdf_pages=10)
    assert "吊牌" in doc.text


def test_txt_undecodable_rejected():
    # 既不是合法 UTF-8，也不是合法 GB18030，必须报错而不是静默产出乱码。
    with pytest.raises(ValueError, match="编码"):
        parse_document("note.txt", b"\xff\xff", max_pdf_pages=10)


# -------------------------------------------------------------------------- csv
def test_csv_rows_flattened():
    doc = parse_document("t.csv", "name,qty\n吊牌,1\n".encode(), max_pdf_pages=10)
    assert "name: 吊牌" in doc.text
    assert "qty: 1" in doc.text
    assert doc.metadata == {"rows": 1}
    assert doc.title == "t"


def test_csv_multiple_rows_one_line_each():
    raw = "name,qty\n吊牌,1\n包装盒,2\n".encode()
    doc = parse_document("t.csv", raw, max_pdf_pages=10)
    assert doc.text.splitlines() == ["name: 吊牌; qty: 1", "name: 包装盒; qty: 2"]
    assert doc.metadata["rows"] == 2


def test_csv_quoted_value_preserved():
    doc = parse_document("t.csv", 'name,desc\n吊牌,"a,b"\n'.encode(), max_pdf_pages=10)
    assert "desc: a,b" in doc.text


def test_csv_extra_cells_preserved():
    # 行宽超过表头时不静默丢数据，多余单元格统一记为「额外列」。
    doc = parse_document("t.csv", "name,qty\n吊牌,1,3\n".encode(), max_pdf_pages=10)
    assert "额外列: 3" in doc.text


def test_csv_header_only_rejected():
    with pytest.raises(ValueError, match="为空"):
        parse_document("t.csv", b"name,qty\n", max_pdf_pages=10)


# ------------------------------------------------------------------------- json
def test_json_flattened_nested():
    payload = {
        "a": {"b": 1},
        "tags": ["吊牌", "包装"],
        "ok": True,
        "note": None,
    }
    doc = parse_document(
        "p.json", json.dumps(payload, ensure_ascii=False).encode(), max_pdf_pages=10
    )
    assert "a.b: 1" in doc.text
    assert "tags[0]: 吊牌" in doc.text
    assert "tags[1]: 包装" in doc.text
    assert "ok: true" in doc.text
    assert "note: null" in doc.text
    assert doc.title == "p"
    assert doc.metadata == {}


def test_json_invalid_rejected():
    with pytest.raises(ValueError, match="JSON"):
        parse_document("p.json", b"{not json", max_pdf_pages=10)


def test_json_empty_object_rejected():
    with pytest.raises(ValueError, match="为空"):
        parse_document("p.json", b"{}", max_pdf_pages=10)


# -------------------------------------------------------------------------- pdf
def test_pdf_with_text_layer():
    doc = parse_document("a.pdf", _make_pdf("return policy tag"), max_pdf_pages=10)
    assert "tag" in doc.text
    assert doc.metadata == {"pages": 1}
    assert doc.title == "a"


def test_pdf_pages_joined_in_order():
    doc = parse_document("a.pdf", _make_pdf("first page", "second page"), max_pdf_pages=10)
    assert doc.text.index("first page") < doc.text.index("second page")
    assert doc.metadata["pages"] == 2


def test_pdf_without_text_layer_fails():
    with pytest.raises(ValueError, match="扫描件"):
        parse_document("scan.pdf", _make_pdf(""), max_pdf_pages=10)


def test_pdf_page_limit_enforced():
    with pytest.raises(ValueError, match="页数"):
        parse_document("a.pdf", _make_pdf("one", "two"), max_pdf_pages=1)


def test_pdf_malformed_rejected():
    with pytest.raises(ValueError, match="PDF"):
        parse_document("a.pdf", b"not a pdf", max_pdf_pages=10)


# ------------------------------------------------------------- 返回值类型约定
def test_returns_frozen_dataclass():
    doc = parse_document("a.md", "正文".encode(), max_pdf_pages=10)
    assert isinstance(doc, ParsedDocument)
    with pytest.raises(FrozenInstanceError):
        doc.text = "改不动"  # type: ignore[misc]
