"""知识库 service 层纯函数单元测试：不依赖数据库，也不访问网络。"""

from __future__ import annotations

from app.modules.knowledge.embedding import EmbeddingUnavailableError
from app.modules.knowledge.service import _GENERIC_FAILURE_ERROR, _failure_message


def test_failure_message_passes_through_chinese_value_error() -> None:
    """中文业务文案（解析/切分主动抛出）应原样展示给用户。"""
    assert _failure_message(ValueError("文档切分后没有可入库的片段")) == (
        "文档切分后没有可入库的片段"
    )


def test_failure_message_hides_english_value_error() -> None:
    """英文底层异常文案（如 pypdf）不得透传，统一中文兜底。"""
    message = _failure_message(ValueError("pdf parser exploded at page 3"))

    assert message == _GENERIC_FAILURE_ERROR
    assert "pdf parser" not in message


def test_failure_message_passes_through_embedding_unavailable() -> None:
    """领域内可控异常（Embedding 不可用）文案按约定就是中文，应透传。"""
    assert _failure_message(EmbeddingUnavailableError("Embedding 服务不可用")) == (
        "Embedding 服务不可用"
    )


def test_failure_message_hides_unexpected_english_exception() -> None:
    assert _failure_message(RuntimeError("boom")) == _GENERIC_FAILURE_ERROR
