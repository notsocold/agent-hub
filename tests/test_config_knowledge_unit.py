"""知识库 / 存储相关配置项的单元测试。"""

from __future__ import annotations

from pathlib import Path

from app.core.config import Settings, is_placeholder_api_key


def test_placeholder_keys_detected() -> None:
    assert is_placeholder_api_key("sk-xxx")
    assert is_placeholder_api_key("")
    assert not is_placeholder_api_key("sk-" + "a" * 40)


def test_knowledge_defaults(monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-xxx")
    s = Settings(database_url="postgresql+psycopg://u:p@localhost/x")
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
