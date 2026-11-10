"""Embedding provider 单元测试。

不依赖数据库、不访问网络：fake 提供方是纯计算，OpenAI 提供方通过
``httpx.MockTransport`` 注入假响应。每个用例都显式隔离 Embedding 环境变量，
避免本机 ``.env`` 或上一条用例的单例缓存影响断言。
"""

from __future__ import annotations

import json
import math

import httpx
import pytest

from app.core.config import Settings, reset_settings_cache
from app.modules.knowledge.embedding import (
    EmbeddingUnavailableError,
    FakeEmbeddingProvider,
    OpenAIEmbeddingProvider,
    get_embedding_provider,
    reset_embedding_provider,
)

# 被测配置的环境变量：统一注入确定值，测试不依赖本机 .env 内容。
_ENV_DEFAULTS = {
    "EMBEDDING_PROVIDER": "auto",
    "EMBEDDING_API_KEY": "",
    "EMBEDDING_BASE_URL": "https://api.openai.com/v1",
    "EMBEDDING_MODEL": "text-embedding-3-small",
    "EMBEDDING_DIM": "1536",
    "EMBEDDING_TIMEOUT_SECONDS": "30",
    "EMBEDDING_BATCH_SIZE": "32",
}

# 形状合法、能通过占位符校验的假密钥（长度 ≥ 20）。
_USABLE_KEY = "sk-" + "a" * 40


@pytest.fixture(autouse=True)
def _isolated_embedding_env(monkeypatch):
    """每个用例前后固定 Embedding 环境变量并清空配置/提供方缓存。"""
    for name, value in _ENV_DEFAULTS.items():
        monkeypatch.setenv(name, value)
    reset_settings_cache()
    reset_embedding_provider()
    yield
    reset_embedding_provider()
    reset_settings_cache()


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _is_zero_vector(vector: list[float]) -> bool:
    return all(value == 0.0 for value in vector)


def _assert_unit_norm(vector: list[float]) -> None:
    assert math.isclose(sum(value * value for value in vector), 1.0, rel_tol=1e-6)


class _RecordingTransport:
    """记录每次请求的假传输；应答由 ``responder`` 决定。"""

    def __init__(self, responder) -> None:
        self._responder = responder
        self.requests: list[httpx.Request] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self._responder(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


# ---------------------------------------------------------------------- fake


async def test_fake_is_deterministic_and_normalized() -> None:
    """同一文本两次结果必须完全一致，且向量已 L2 归一化。"""
    provider = FakeEmbeddingProvider(1536)
    first, second = await provider.embed(["退货政策"]), await provider.embed(["退货政策"])
    assert first == second
    assert len(first[0]) == 1536
    _assert_unit_norm(first[0])


async def test_fake_ranks_related_above_unrelated() -> None:
    """归一化后点积即余弦：相关文本的分数必须高于无关文本。"""
    provider = FakeEmbeddingProvider(1536)
    query, near, far = (
        await provider.embed(["退货需要保留吊牌吗"]),
        await provider.embed(["退货需要保留吊牌吗以及包装"]),
        await provider.embed(["服务器磁盘告警阈值"]),
    )
    assert _dot(query[0], near[0]) > _dot(query[0], far[0])


async def test_fake_empty_input_returns_zero_vectors() -> None:
    """空列表与空白文本返回全零向量，不触发除零。"""
    provider = FakeEmbeddingProvider(8)
    assert await provider.embed([]) == []
    blank = await provider.embed(["", "   ，。！？ "])
    assert len(blank) == 2
    for vector in blank:
        assert len(vector) == 8
        assert _is_zero_vector(vector)


async def test_fake_single_char_and_punctuation_stay_searchable() -> None:
    """单个汉字也要产生 token；标点/空白只作分隔，不参与 token。"""
    provider = FakeEmbeddingProvider(64)
    vectors = await provider.embed(["退", "退货，政策！"])
    single, punctuated = vectors[0], vectors[1]
    assert not _is_zero_vector(single)
    assert not _is_zero_vector(punctuated)
    _assert_unit_norm(single)
    _assert_unit_norm(punctuated)


async def test_fake_english_is_case_insensitive() -> None:
    """英文/数字按小写词切分，大小写不应改变向量。"""
    provider = FakeEmbeddingProvider(128)
    upper, lower = (
        await provider.embed(["Refund Policy v2"]),
        await provider.embed(["refund policy v2"]),
    )
    assert upper == lower
    _assert_unit_norm(upper[0])


async def test_fake_rejects_non_positive_dim() -> None:
    """维度必须为正；否则取模无意义。"""
    with pytest.raises(ValueError, match="维度"):
        FakeEmbeddingProvider(0)


# ------------------------------------------------------------------ 选择逻辑


def test_placeholder_key_selects_fake_with_auto(monkeypatch) -> None:
    """auto + 占位符密钥 → fake，绝不能把占位符当真密钥去联网。"""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "auto")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-xxx")
    assert isinstance(get_embedding_provider(), FakeEmbeddingProvider)


def test_empty_key_selects_fake_with_auto(monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "auto")
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    provider = get_embedding_provider()
    assert isinstance(provider, FakeEmbeddingProvider)
    assert provider.dim == 1536


def test_usable_key_selects_openai_with_auto(monkeypatch) -> None:
    """auto + 可用密钥 → 真实提供方（构造阶段不发起任何网络请求）。"""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "auto")
    monkeypatch.setenv("EMBEDDING_API_KEY", _USABLE_KEY)
    provider = get_embedding_provider()
    assert isinstance(provider, OpenAIEmbeddingProvider)
    assert not isinstance(provider, FakeEmbeddingProvider)


def test_forced_fake_ignores_usable_key(monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    monkeypatch.setenv("EMBEDDING_API_KEY", _USABLE_KEY)
    assert isinstance(get_embedding_provider(), FakeEmbeddingProvider)


def test_forced_openai_without_usable_key_raises(monkeypatch) -> None:
    """显式指定 openai 却无可用密钥属于配置错误，不静默降级。"""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-xxx")
    with pytest.raises(ValueError, match="EMBEDDING_API_KEY"):
        get_embedding_provider()


def test_provider_is_cached_and_reset_clears_it(monkeypatch) -> None:
    """进程级单例：重复获取同一实例，reset 后重建。"""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    first = get_embedding_provider()
    second = get_embedding_provider()
    assert first is second

    reset_embedding_provider()
    third = get_embedding_provider()
    assert third is not first
    assert isinstance(third, FakeEmbeddingProvider)


def test_explicit_settings_argument_is_used() -> None:
    """显式传入 Settings 时无需读取环境变量/缓存。"""
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://u:p@localhost/x",
        embedding_provider="fake",
        embedding_dim=16,
        embedding_api_key="",
    )
    provider = get_embedding_provider(settings)
    assert isinstance(provider, FakeEmbeddingProvider)
    assert provider.dim == 16


# ---------------------------------------------------------------- OpenAI 路径


def _openai_provider(
    transport: httpx.MockTransport, *, batch_size: int = 2
) -> OpenAIEmbeddingProvider:
    return OpenAIEmbeddingProvider(
        api_key=_USABLE_KEY,
        base_url="https://embed.example.test/v1/",
        model="test-embedding",
        dim=3,
        timeout=5.0,
        batch_size=batch_size,
        transport=transport,
    )


async def _ok_responder(request: httpx.Request) -> httpx.Response:
    """按输入文本长度返回可区分的向量，便于断言分批拼回后的顺序。"""
    payload = json.loads(request.content)
    return httpx.Response(
        200,
        json={"data": [{"embedding": [float(len(text)), 1.0, 2.0]} for text in payload["input"]]},
    )


async def test_openai_embed_batches_and_parses() -> None:
    """按 batch_size 分批 POST，拼回去的顺序与输入一致。"""
    recorder = _RecordingTransport(_ok_responder)
    provider = _openai_provider(recorder.transport)

    # 三段的长度分别是 1/2/3，用它区分向量来自哪一段输入。
    vectors = await provider.embed(["甲", "乙乙", "丙丙丙"])

    assert len(vectors) == 3
    assert all(len(vector) == 3 for vector in vectors)
    assert [int(vector[0]) for vector in vectors] == [1, 2, 3]
    payloads = [json.loads(record.content) for record in recorder.requests]
    assert [len(payload["input"]) for payload in payloads] == [2, 1]
    assert [payload["input"] for payload in payloads] == [["甲", "乙乙"], ["丙丙丙"]]
    assert all(payload["model"] == "test-embedding" for payload in payloads)
    assert [str(record.url) for record in recorder.requests] == [
        "https://embed.example.test/v1/embeddings",
        "https://embed.example.test/v1/embeddings",
    ]
    assert recorder.requests[0].headers["authorization"] == f"Bearer {_USABLE_KEY}"


async def test_openai_embed_empty_input_skips_network() -> None:
    recorder = _RecordingTransport(_ok_responder)
    provider = _openai_provider(recorder.transport)

    assert await provider.embed([]) == []
    assert recorder.requests == []


async def test_openai_embed_raises_on_non_2xx() -> None:
    async def _fail(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    provider = _openai_provider(httpx.MockTransport(_fail))
    with pytest.raises(EmbeddingUnavailableError, match="503"):
        await provider.embed(["任意文本"])


async def test_openai_embed_raises_on_network_error() -> None:
    async def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = _openai_provider(httpx.MockTransport(_boom))
    with pytest.raises(EmbeddingUnavailableError, match="Embedding"):
        await provider.embed(["任意文本"])


async def test_openai_embed_raises_on_invalid_json() -> None:
    async def _garbage(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    provider = _openai_provider(httpx.MockTransport(_garbage))
    with pytest.raises(EmbeddingUnavailableError):
        await provider.embed(["任意文本"])


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},  # 条数与输入不符
        {"data": [{"embedding": [1.0, 2.0]}]},  # 维度与 embedding_dim 不符
        {"data": [{"embedding": "not-a-list"}]},  # 向量不是数组
        {"data": [{"embedding": ["x", "y", "z"]}]},  # 向量元素非数值
        {"unexpected": "shape"},  # 缺少 data
    ],
)
async def test_openai_embed_raises_on_malformed_payload(payload: dict) -> None:
    async def _malformed(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    provider = _openai_provider(httpx.MockTransport(_malformed))
    with pytest.raises(EmbeddingUnavailableError):
        await provider.embed(["任意文本"])


def test_openai_provider_rejects_non_positive_batch_size() -> None:
    transport = httpx.MockTransport(_ok_responder)
    with pytest.raises(ValueError, match="batch"):
        _openai_provider(transport, batch_size=0)
