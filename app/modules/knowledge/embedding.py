"""Embedding 提供方：确定性假向量与 OpenAI 兼容接口。

知识库只依赖这里的 :class:`EmbeddingProvider` 协议：

- :class:`FakeEmbeddingProvider`：零依赖、确定性的词袋向量，供测试与无密钥开发环境使用；
- :class:`OpenAIEmbeddingProvider`：按批 POST ``{base_url}/embeddings``，失败统一抛
  :class:`EmbeddingUnavailableError`。

选择逻辑见 :func:`get_embedding_provider`。fake 模式只保证排序有意义，
绝对分数不可解释（短 query 对长 chunk 的余弦存在上界），检索层需要把阈值降为 0，
详见设计文档 §8.2。
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Protocol

import httpx

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# 中文按字符 bigram 切分；标点/空白不会命中该正则，天然作为分隔符。
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
# 英文/数字按词切分（匹配前整体小写）。
_LATIN_RUN = re.compile(r"[a-z0-9]+")


class EmbeddingUnavailableError(Exception):
    """Embedding 服务不可用或响应结构异常；上层据此返回 503 或记为失败。"""


class EmbeddingProvider(Protocol):
    """Embedding 提供方协议：批量文本进、同序向量出。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """把一批文本转成等长、同序的向量列表。"""
        ...


def _tokenize(text: str) -> list[str]:
    """切分 token：中文 bigram，英文/数字小写词，标点与空白不计。"""
    tokens: list[str] = []
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            # 单字无法组成 bigram，保留单字，避免短查询退化成全零向量。
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    tokens.extend(_LATIN_RUN.findall(text.lower()))
    return tokens


def _hashed_token(token: str, dim: int) -> tuple[int, float]:
    """token → (维度下标, ±1)：带符号哈希用于抵消碰撞偏置。"""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    index = int.from_bytes(digest, "big") % dim
    sign = 1.0 if digest[0] % 2 == 0 else -1.0
    return index, sign


class FakeEmbeddingProvider:
    """确定性的词袋假向量：相同文本永远得到相同向量，且已 L2 归一化。

    算法（设计文档 §8.2）：中文 bigram + 英文小写词 → ``blake2b`` 带符号哈希到
    ``dim`` 维 → 词频累加 → L2 归一化。空输入返回全零向量，不做除零。

    该模型下短 query 对长 chunk 的余弦存在上界，绝对分数不可解释；
    检索层需要在 fake 模式把阈值降为 0，只依赖排序。
    """

    def __init__(self, dim: int) -> None:
        if dim <= 0:
            raise ValueError(f"Embedding 维度必须为正数，当前为 {dim}")
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        # 词频累加；使用普通 dict 保证 token 顺序确定，向量逐位可复现。
        counts: dict[str, int] = {}
        for token in _tokenize(text):
            counts[token] = counts.get(token, 0) + 1
        vector = [0.0] * self.dim
        for token, count in counts.items():
            index, sign = _hashed_token(token, self.dim)
            vector[index] += sign * count
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            # 空文本或纯标点：返回全零向量，避免除零。
            return vector
        return [value / norm for value in vector]


class OpenAIEmbeddingProvider:
    """OpenAI 兼容 ``/embeddings`` 接口实现；按批发送，不做重试。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        dim: int,
        timeout: float,
        batch_size: int,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if dim <= 0:
            raise ValueError(f"Embedding 维度必须为正数，当前为 {dim}")
        if batch_size <= 0:
            raise ValueError(f"Embedding batch_size 必须为正数，当前为 {batch_size}")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dim = dim
        self._timeout = timeout
        self._batch_size = batch_size
        # 仅测试注入（httpx.MockTransport）；生产走默认传输层。
        self._transport = transport

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {"Content-Type": "application/json"}
        # 兼容 Ollama/vLLM 等无需鉴权的本地服务：只有存在密钥时才带 Authorization。
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(self._timeout),
        ) as client:
            for start in range(0, len(texts), self._batch_size):
                batch = texts[start : start + self._batch_size]
                vectors.extend(await self._embed_batch(client, headers, batch))
        return vectors

    async def _embed_batch(
        self, client: httpx.AsyncClient, headers: dict[str, str], batch: list[str]
    ) -> list[list[float]]:
        try:
            response = await client.post(
                f"{self._base_url}/embeddings",
                json={"model": self._model, "input": batch},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            # 连接失败/超时等传输层错误视为服务不可用，交由上层降级或返回 503。
            raise EmbeddingUnavailableError("Embedding 请求失败，无法连接服务") from exc
        if not 200 <= response.status_code < 300:
            raise EmbeddingUnavailableError(f"Embedding 接口返回 HTTP {response.status_code}")
        return self._parse_vectors(response, expected=len(batch))

    def _parse_vectors(self, response: httpx.Response, *, expected: int) -> list[list[float]]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise EmbeddingUnavailableError("Embedding 接口返回的不是合法 JSON") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) != expected:
            raise EmbeddingUnavailableError("Embedding 接口返回结构异常：data 条数与输入不符")
        vectors: list[list[float]] = []
        for item in data:
            embedding = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(embedding, list) or len(embedding) != self._dim:
                raise EmbeddingUnavailableError(
                    f"Embedding 接口返回结构异常：向量维度应为 {self._dim}"
                )
            try:
                vectors.append([float(value) for value in embedding])
            except (TypeError, ValueError) as exc:
                raise EmbeddingUnavailableError(
                    "Embedding 接口返回结构异常：向量元素非数值"
                ) from exc
        return vectors


_provider: EmbeddingProvider | None = None


def _build_provider(config: Settings) -> EmbeddingProvider:
    """按配置构建提供方；显式 openai 但密钥不可用属于配置错误。"""
    if config.embedding_provider == "fake":
        logger.info("Embedding 使用 fake 提供方 dim=%s（分数不可解释）", config.embedding_dim)
        return FakeEmbeddingProvider(config.embedding_dim)
    if config.embedding_provider == "openai" and not config.embedding_key_usable:
        # 不静默降级：显式要求真实接口却没密钥，必须让部署方看到配置错误。
        raise ValueError(
            "EMBEDDING_PROVIDER=openai 时必须配置可用的 EMBEDDING_API_KEY（当前为空或占位符）"
        )
    if not config.embedding_key_usable:
        logger.info("Embedding 未配置可用密钥，回退 fake 提供方 dim=%s", config.embedding_dim)
        return FakeEmbeddingProvider(config.embedding_dim)
    logger.info(
        "Embedding 使用 OpenAI 兼容接口 model=%s base_url=%s",
        config.embedding_model,
        config.embedding_base_url,
    )
    return OpenAIEmbeddingProvider(
        config.embedding_api_key,
        config.embedding_base_url,
        config.embedding_model,
        config.embedding_dim,
        config.embedding_timeout_seconds,
        config.embedding_batch_size,
    )


def get_embedding_provider(settings: Settings | None = None) -> EmbeddingProvider:
    """按配置返回进程级单例；切换配置后需 :func:`reset_embedding_provider`。"""
    global _provider
    if _provider is None:
        _provider = _build_provider(settings or get_settings())
    return _provider


def reset_embedding_provider() -> None:
    """测试辅助：清空提供方单例缓存（下次调用按当前配置重建）。"""
    global _provider
    _provider = None


__all__ = [
    "EmbeddingProvider",
    "EmbeddingUnavailableError",
    "FakeEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "get_embedding_provider",
    "reset_embedding_provider",
]
