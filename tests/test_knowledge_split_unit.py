"""知识库文本切分单元测试。

不依赖数据库、不访问网络：切分是纯函数，所有断言都直接落在真实输出上。
用例覆盖 Markdown 标题（含嵌套路径）、段落聚合、跨节不合并、长段落按句切分、
句子超长硬切、块间 overlap、尾部小块并入、块数截断与空输入。
"""

from __future__ import annotations

import itertools

import pytest

from app.modules.knowledge.splitting import Chunk, split_text

# 一段足够长的中文句子：9 字符，便于推算 size/overlap 边界。
_SENTENCE = "这是一个测试句子。"


# ---------------------------------------------------------------- 任务书四个用例
def test_markdown_headings_recorded():
    text = "# 售后\n\n退货要保留吊牌。\n\n# 产品\n\n新品发布。"
    chunks = split_text(text, size=800, overlap=0, max_chunks=100)
    assert [c.heading for c in chunks] == ["售后", "产品"]
    assert chunks[0].index == 0 and chunks[1].index == 1
    assert [c.content for c in chunks] == ["退货要保留吊牌。", "新品发布。"]
    assert chunks[0].metadata == {"heading": "售后"}
    assert chunks[1].metadata == {"heading": "产品"}


def test_long_paragraph_split_by_sentence():
    text = "。" * 2000 + "。"
    chunks = split_text(text, size=100, overlap=10, max_chunks=100)
    assert all(len(c.content) <= 140 for c in chunks)


def test_short_tail_merged():
    chunks = split_text("甲" * 790 + "\n\n" + "乙" * 5, size=800, overlap=0, max_chunks=100)
    assert len(chunks) == 1


def test_max_chunks_truncates():
    chunks = split_text("段落。\n\n" * 50, size=20, overlap=0, max_chunks=3)
    assert len(chunks) == 3


# -------------------------------------------------------------------- 输入与标题
def test_empty_and_whitespace_only_returns_empty():
    assert split_text("", size=800, overlap=0, max_chunks=100) == []
    assert split_text(" \n\n\t  \n", size=800, overlap=0, max_chunks=100) == []


def test_nested_heading_path_uses_angle_separator():
    text = (
        "# 售后\n\n一级内容。\n\n"
        "## 退货\n\n二级内容。\n\n"
        "### 吊牌\n\n三级内容。\n\n"
        "## 换货\n\n二级二内容。\n\n"
        "# 产品\n\n顶级二内容。"
    )
    chunks = split_text(text, size=800, overlap=0, max_chunks=100)
    assert [c.heading for c in chunks] == [
        "售后",
        "售后 > 退货",
        "售后 > 退货 > 吊牌",
        "售后 > 换货",
        "产品",
    ]


def test_preamble_before_first_heading_has_empty_heading():
    text = "前言内容。\n\n# 标题\n\n正文内容。"
    chunks = split_text(text, size=800, overlap=0, max_chunks=100)
    assert [c.heading for c in chunks] == ["", "标题"]
    assert chunks[0].metadata["heading"] == ""


def test_heading_only_section_produces_no_chunk():
    text = "# 空节\n\n## 更深空节\n\n# 有内容\n\n正文内容。"
    chunks = split_text(text, size=800, overlap=0, max_chunks=100)
    assert [c.heading for c in chunks] == ["有内容"]
    assert chunks[0].content == "正文内容。"


def test_closing_hashes_are_stripped_from_heading_text():
    text = "## 退货政策 ##\n\n保留吊牌。"
    chunks = split_text(text, size=800, overlap=0, max_chunks=100)
    assert chunks[0].heading == "退货政策"


def test_multiline_paragraph_is_one_paragraph():
    text = "# 标题\n\n第一行\n第二行\n\n下一段。"
    chunks = split_text(text, size=800, overlap=0, max_chunks=100)
    assert len(chunks) == 1
    assert chunks[0].content == "第一行\n第二行\n\n下一段。"


# ------------------------------------------------------------------ 聚合与跨节
def test_adjacent_paragraphs_in_same_section_merge():
    first = "甲" * 100
    second = "乙" * 100
    text = "# 标题\n\n" + first + "\n\n" + second
    chunks = split_text(text, size=300, overlap=0, max_chunks=100)
    assert len(chunks) == 1
    assert chunks[0].content == first + "\n\n" + second
    assert chunks[0].heading == "标题"
    assert chunks[0].char_start == text.index(first)


def test_no_merge_across_sections():
    text = "# 甲节\n\n" + "甲" * 100 + "\n\n# 乙节\n\n" + "乙" * 100
    chunks = split_text(text, size=300, overlap=0, max_chunks=100)
    assert len(chunks) == 2
    assert [c.heading for c in chunks] == ["甲节", "乙节"]
    assert chunks[0].content == "甲" * 100
    assert chunks[1].content == "乙" * 100


def test_short_paragraph_before_long_paragraph_is_aggregated():
    text = "# 甲节\n\n短段落。\n\n" + "长句内容。" * 200
    chunks = split_text(text, size=100, overlap=10, max_chunks=100)
    # 短段落与超长段落的首片同属一节，应合并进第一块，而不是留下 4 字符碎片。
    assert chunks[0].content.startswith("短段落。")
    assert len(chunks[0].content) >= 50
    assert all(len(c.content) <= 100 for c in chunks)
    for chunk in chunks:
        assert text[chunk.char_start : chunk.char_start + len(chunk.content)] == chunk.content


def test_char_start_points_into_original_text():
    first = "甲" * 40
    second = "乙" * 40
    text = "# 标题\n\n" + first + "\n\n" + second
    chunks = split_text(text, size=200, overlap=0, max_chunks=100)
    assert chunks[0].char_start == text.index(first)
    assert text[chunks[0].char_start : chunks[0].char_start + len(chunks[0].content)] == (
        chunks[0].content
    )


def test_char_start_with_crlf_line_endings():
    body = "正文内容。"
    text = "# 标题\r\n\r\n" + body
    chunks = split_text(text, size=800, overlap=0, max_chunks=100)
    assert chunks[0].content == body
    assert chunks[0].char_start == text.index(body)
    assert text[chunks[0].char_start :] == chunks[0].content


# ------------------------------------------------------------ 长段落：句切与硬切
def test_long_paragraph_splits_on_sentence_terminators():
    text = "# 长文\n\n" + _SENTENCE * 20
    chunks = split_text(text, size=50, overlap=0, max_chunks=100)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.content) <= 50
        assert chunk.content.endswith("。")


def test_single_overlong_sentence_is_hard_split():
    text = "甲" * 130 + "。"
    chunks = split_text(text, size=50, overlap=0, max_chunks=100)
    assert len(chunks) >= 3
    assert all(len(c.content) <= 50 for c in chunks)
    assert "".join(c.content for c in chunks) == text


def test_hard_split_balances_pieces_without_tiny_internal_chunks():
    text = "甲" * 130 + "。"
    chunks = split_text(text, size=50, overlap=0, max_chunks=100)
    # 131 字符按 3 份均分，不应出现 1~2 字符的碎片。
    assert all(len(c.content) >= 40 for c in chunks)


def test_overlap_between_split_chunks():
    text = "甲" * 130 + "。"
    chunks = split_text(text, size=50, overlap=10, max_chunks=100)
    assert len(chunks) >= 3
    for previous, current in itertools.pairwise(chunks):
        assert current.content.startswith(previous.content[-10:])
    for chunk in chunks:
        assert len(chunk.content) <= 50
        assert text[chunk.char_start : chunk.char_start + len(chunk.content)] == chunk.content


def test_no_overlap_between_sections():
    text = "# 甲节\n\n" + "甲" * 200 + "\n\n# 乙节\n\n" + "乙" * 200
    chunks = split_text(text, size=50, overlap=10, max_chunks=100)
    headings = [c.heading for c in chunks]
    assert "甲节" in headings and "乙节" in headings
    split_point = headings.index("乙节")
    assert chunks[split_point].content.startswith("乙")
    assert not chunks[split_point].content.startswith("甲")


# -------------------------------------------------------------------- 尾部合并
def test_short_tail_merges_into_previous_chunk():
    text = "甲" * 795 + "\n\n" + "乙" * 5
    chunks = split_text(text, size=800, overlap=0, max_chunks=100)
    assert len(chunks) == 1
    assert chunks[0].content.startswith("甲" * 795)
    assert chunks[0].content.endswith("乙" * 5)
    assert chunks[0].char_start == 0


def test_short_tail_does_not_merge_across_sections():
    text = "# 甲节\n\n" + "甲" * 795 + "\n\n# 乙节\n\n" + "乙" * 5
    chunks = split_text(text, size=800, overlap=0, max_chunks=100)
    assert [c.heading for c in chunks] == ["甲节", "乙节"]
    assert chunks[1].content == "乙" * 5


def test_tail_of_exactly_twenty_chars_is_not_merged():
    text = "甲" * 790 + "\n\n" + "乙" * 20
    chunks = split_text(text, size=800, overlap=0, max_chunks=100)
    # 790 + 2 + 20 已经超过 800，尾部正好 20 字符不触发「不足 20」规则。
    assert len(chunks) == 2
    assert chunks[1].content == "乙" * 20


# ---------------------------------------------------------------------- 截断
def test_truncated_flag_only_on_last_kept_chunk():
    text = "".join(f"# 第{i}节\n\n正文内容{i}。\n\n" for i in range(10))
    chunks = split_text(text, size=800, overlap=0, max_chunks=3)
    assert len(chunks) == 3
    assert [c.index for c in chunks] == [0, 1, 2]
    assert "truncated" not in chunks[0].metadata
    assert "truncated" not in chunks[1].metadata
    assert chunks[2].metadata["truncated"] is True
    assert chunks[2].heading == "第2节"


def test_no_truncated_flag_when_under_limit():
    chunks = split_text("正文。", size=800, overlap=0, max_chunks=100)
    assert len(chunks) == 1
    assert "truncated" not in chunks[0].metadata


def test_zero_max_chunks_returns_empty():
    assert split_text("正文。", size=800, overlap=0, max_chunks=0) == []


# ------------------------------------------------------------------ 参数与类型
def test_chunk_is_dataclass_with_expected_fields():
    chunk = Chunk(
        content="正文",
        index=0,
        heading="",
        char_start=0,
        metadata={"heading": ""},
    )
    assert chunk.content == "正文"
    assert chunk.metadata == {"heading": ""}


@pytest.mark.parametrize("size", [0, -1])
def test_non_positive_size_rejected(size):
    with pytest.raises(ValueError):
        split_text("正文。", size=size, overlap=0, max_chunks=100)


def test_negative_overlap_rejected():
    with pytest.raises(ValueError):
        split_text("正文。", size=100, overlap=-1, max_chunks=100)


def test_overlap_not_smaller_than_size_rejected():
    with pytest.raises(ValueError):
        split_text("正文。", size=100, overlap=100, max_chunks=100)
