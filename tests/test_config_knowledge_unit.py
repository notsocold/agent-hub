"""知识库 / 存储相关配置项的单元测试。

构造 ``Settings`` 时统一禁用 ``.env`` 并清空被测字段的环境变量，
确保断言的是代码默认值，不受本机 .env 或后续 conftest 强制注入的影响。
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import Settings, is_placeholder_api_key

# 被测默认值字段对应的环境变量；构造 Settings 前逐个清空，避免环境注入干扰断言。
_DEFAULT_ENV_VARS = (
    "KNOWLEDGE_ENABLED",
    "KNOWLEDGE_DIR",
    "KNOWLEDGE_MAX_UPLOAD_BYTES",
    "KNOWLEDGE_CHUNK_SIZE",
    "KNOWLEDGE_CHUNK_OVERLAP",
    "KNOWLEDGE_MAX_CHUNKS_PER_DOCUMENT",
    "KNOWLEDGE_MAX_PDF_PAGES",
    "STORAGE_BACKEND",
    "S3_ENDPOINT_URL",
    "S3_BUCKET",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "S3_REGION",
    "S3_PREFIX",
    "EMBEDDING_PROVIDER",
    "EMBEDDING_TIMEOUT_SECONDS",
    "EMBEDDING_BATCH_SIZE",
)


def _default_settings(monkeypatch) -> Settings:
    """只读取代码默认值构造 Settings：禁用 .env 并清空被测字段的环境变量。"""
    for env_name in _DEFAULT_ENV_VARS:
        monkeypatch.delenv(env_name, raising=False)
    return Settings(_env_file=None, database_url="postgresql+psycopg://u:p@localhost/x")


def test_placeholder_keys_detected() -> None:
    assert is_placeholder_api_key("sk-xxx")
    assert is_placeholder_api_key("")
    assert not is_placeholder_api_key("sk-" + "a" * 40)
    # 长度超过 20，只能由正则分支识别；同时验证大小写不敏感。
    assert is_placeholder_api_key("sk-test-" + "0" * 20)
    assert is_placeholder_api_key("sk-YOUR-KEY-" + "0" * 20)
    assert is_placeholder_api_key("sk-placeholder-" + "0" * 20)


def test_knowledge_defaults(monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-xxx")
    s = _default_settings(monkeypatch)
    assert s.knowledge_enabled is True
    assert s.knowledge_dir == Path("data/knowledge")
    assert s.knowledge_max_upload_bytes == 10 * 1024 * 1024
    assert s.knowledge_chunk_size == 800
    assert s.knowledge_chunk_overlap == 80
    assert s.knowledge_max_chunks_per_document == 5000
    assert s.knowledge_max_pdf_pages == 500
    assert s.storage_backend == "local"
    assert s.s3_endpoint_url == ""
    assert s.s3_bucket == ""
    assert s.s3_access_key == ""
    assert s.s3_secret_key == ""
    assert s.s3_region == "us-east-1"
    assert s.s3_prefix == "knowledge/"
    assert s.embedding_provider == "auto"
    assert s.embedding_timeout_seconds == 30
    assert s.embedding_batch_size == 32
    assert s.embedding_key_usable is False


def test_embedding_key_usable_with_real_key(monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-" + "a" * 40)
    s = Settings(_env_file=None, database_url="postgresql+psycopg://u:p@localhost/x")
    assert s.embedding_key_usable is True
