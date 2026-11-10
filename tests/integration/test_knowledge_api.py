"""M4 知识库 API 层集成测试：7 个端点、字段契约、错误码与能力开关。

全部用例走真实 HTTP（``httpx.ASGITransport``）与真实 PostgreSQL；原文落盘到
``data/test-knowledge``，向量使用确定性 fake provider，不访问网络。服务层内部行为
已由 ``test_knowledge_service.py`` 覆盖，这里只断言 HTTP 契约与映射。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import reset_settings_cache
from app.core.security import create_access_token
from app.db.models.knowledge import KnowledgeDocument
from app.db.models.user import User
from app.modules.knowledge.embedding import EmbeddingUnavailableError
from tests.conftest import auth_headers

DOCUMENTS_URL = "/api/v1/knowledge/documents"
SEARCH_URL = "/api/v1/knowledge/search"
REBUILD_URL = "/api/v1/knowledge/rebuild"

# 前端冻结契约（spec §7.1 / §7.4）：字段集合必须精确一致，不多不少。
DOCUMENT_KEYS = frozenset(
    {
        "id",
        "title",
        "source_uri",
        "category",
        "status",
        "chunk_count",
        "size_bytes",
        "owner",
        "error",
        "created_at",
        "updated_at",
    }
)
HIT_KEYS = frozenset(
    {
        "chunk_id",
        "document_id",
        "title",
        "document_title",
        "document_filename",
        "category",
        "chunk_index",
        "content",
        "score",
        "updated_at",
    }
)
STATS_KEYS = frozenset({"documents", "chunks", "ready", "indexing", "pending", "failed"})


@pytest.fixture
def auth(user: User) -> dict[str, str]:
    """普通用户请求头；与 ``admin_auth`` 同口径，直接签发 access token。"""
    return auth_headers(create_access_token(user.id).token)


class _BrokenEmbeddingProvider:
    """向量化失败注入：模拟真实 embedding provider 网络/鉴权不可用。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailableError("Embedding 服务不可用")


async def _upload(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    filename: str = "doc.md",
    content: bytes = "# 标题\n\n正文内容。".encode(),
    **form: str,
) -> httpx.Response:
    return await client.post(
        DOCUMENTS_URL,
        files={"file": (filename, content, "text/markdown")},
        data=form,
        headers=headers,
    )


async def _document_count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(KnowledgeDocument)) or 0)


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


# --------------------------------------------------------------- 上传/列表/详情/删除
async def test_upload_list_detail_delete_roundtrip(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    user: User,
    fake_storage: Any,
    fake_embedding: Any,
) -> None:
    content = "# 退货\n\n退货需要保留吊牌。".encode()
    created = await _upload(
        client, auth, filename="return.md", content=content, category="售后", title="退货政策"
    )

    assert created.status_code == 201, created.text
    document = created.json()
    assert set(document) == DOCUMENT_KEYS
    assert document["status"] == "ready"
    assert document["chunk_count"] >= 1
    assert document["size_bytes"] == len(content)
    assert document["source_uri"] == "return.md"
    assert document["category"] == "售后"
    assert document["error"] is None
    assert document["owner"] == {"id": str(user.id), "display_name": "测试用户"}

    listing = await client.get(DOCUMENTS_URL, headers=auth)
    assert listing.status_code == 200, listing.text
    page = listing.json()
    assert [item["id"] for item in page["items"]] == [document["id"]]
    assert page["has_more"] is False
    assert page["next_cursor"] is None
    assert set(page["stats"]) == STATS_KEYS
    assert page["stats"]["documents"] == 1
    assert page["stats"]["chunks"] == document["chunk_count"]
    assert page["stats"]["ready"] == 1

    detail = await client.get(f"{DOCUMENTS_URL}/{document['id']}", headers=auth)
    assert detail.status_code == 200
    assert detail.json()["id"] == document["id"]
    assert detail.json()["title"] == "退货政策"

    deleted = await client.delete(f"{DOCUMENTS_URL}/{document['id']}", headers=auth)
    assert deleted.status_code == 204
    assert deleted.content == b""

    missing = await client.get(f"{DOCUMENTS_URL}/{document['id']}", headers=auth)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


async def test_list_stats_shape(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    fake_storage: Any,
    fake_embedding: Any,
) -> None:
    response = await client.get(DOCUMENTS_URL, headers=auth)

    assert response.status_code == 200, response.text
    page = response.json()
    assert page["items"] == []
    assert set(page["stats"]) == STATS_KEYS
    assert page["stats"] == {
        "documents": 0,
        "chunks": 0,
        "ready": 0,
        "indexing": 0,
        "pending": 0,
        "failed": 0,
    }


async def test_oversize_upload_returns_413(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    db: AsyncSession,
    fake_storage: Any,
    fake_embedding: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KNOWLEDGE_MAX_UPLOAD_BYTES", "10")
    reset_settings_cache()
    try:
        response = await _upload(client, auth, content=b"x" * 100)
    finally:
        reset_settings_cache()

    assert response.status_code == 413, response.text
    error = response.json()["error"]
    assert error["code"] == "payload_too_large"
    assert "上限" in error["message"]
    assert await _document_count(db) == 0


async def test_unsupported_extension_returns_422(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    db: AsyncSession,
    fake_storage: Any,
    fake_embedding: Any,
) -> None:
    response = await _upload(client, auth, filename="malware.exe", content=b"x")

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert "不支持" in error["message"]
    assert await _document_count(db) == 0


async def test_duplicate_upload_returns_200_with_same_id(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    db: AsyncSession,
    fake_storage: Any,
    fake_embedding: Any,
) -> None:
    content = "# 幂等\n\n同一份内容只建一行。".encode()

    first = await _upload(client, auth, content=content)
    second = await _upload(client, auth, content=content)

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert first.json()["id"] == second.json()["id"]
    assert await _document_count(db) == 1


async def test_uppercase_markdown_extension_is_ready(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    fake_storage: Any,
    fake_embedding: Any,
) -> None:
    response = await _upload(
        client, auth, filename="GUIDE.MD", content="# 大写\n\n扩展名大小写不影响入库。".encode()
    )

    assert response.status_code == 201, response.text
    document = response.json()
    assert document["status"] == "ready"
    assert document["source_uri"] == "GUIDE.MD"
    # 存储键统一小写扩展名。
    assert await fake_storage.exists(f"knowledge/{document['id']}.md")


async def test_zero_byte_file_marks_failed_with_chinese_error(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    fake_storage: Any,
    fake_embedding: Any,
) -> None:
    response = await _upload(client, auth, filename="empty.md", content=b"")

    assert response.status_code == 201, response.text
    document = response.json()
    assert document["status"] == "failed"
    assert document["chunk_count"] == 0
    assert document["error"]
    assert document["error"] != ""
    assert _has_cjk(document["error"])


async def test_retry_returns_document_object(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    fake_storage: Any,
    fake_embedding: Any,
) -> None:
    created = await _upload(client, auth, filename="empty-retry.md", content=b"")
    document_id = created.json()["id"]

    retried = await client.post(f"{DOCUMENTS_URL}/{document_id}/retry", headers=auth)

    assert retried.status_code == 200, retried.text
    document = retried.json()
    assert set(document) == DOCUMENT_KEYS
    assert document["id"] == document_id
    assert document["status"] == "failed"
    assert document["error"] and _has_cjk(document["error"])


# ------------------------------------------------------------------ 权限与归属
async def test_other_users_document_is_404(
    client: httpx.AsyncClient, auth: dict[str, str], seeded_docs: dict
) -> None:
    document_id = seeded_docs["other"].id

    detail = await client.get(f"{DOCUMENTS_URL}/{document_id}", headers=auth)
    assert detail.status_code == 404
    assert detail.json()["error"]["code"] == "not_found"

    deleted = await client.delete(f"{DOCUMENTS_URL}/{document_id}", headers=auth)
    assert deleted.status_code == 404

    retried = await client.post(f"{DOCUMENTS_URL}/{document_id}/retry", headers=auth)
    assert retried.status_code == 404


async def test_rebuild_forbidden_for_normal_user(
    client: httpx.AsyncClient, auth: dict[str, str], seeded_docs: dict
) -> None:
    response = await client.post(REBUILD_URL, json={"scope": "all"}, headers=auth)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_owner_scope_all_forbidden_for_normal_user(
    client: httpx.AsyncClient, auth: dict[str, str], seeded_docs: dict
) -> None:
    response = await client.get(f"{DOCUMENTS_URL}?owner_scope=all", headers=auth)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_system_scope_upload_requires_admin(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    admin_auth: dict[str, str],
    fake_storage: Any,
    fake_embedding: Any,
) -> None:
    forbidden = await _upload(
        client, auth, filename="notice.md", content="# 公告\n\n内容".encode(), scope="system"
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"

    allowed = await _upload(
        client, admin_auth, filename="notice.md", content="# 公告\n\n内容".encode(), scope="system"
    )
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["owner"] is None


async def test_admin_owner_scope_all_lists_every_document(
    client: httpx.AsyncClient, admin_auth: dict[str, str], seeded_docs: dict
) -> None:
    response = await client.get(f"{DOCUMENTS_URL}?owner_scope=all", headers=admin_auth)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["items"]) == 3
    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id[str(seeded_docs["other"].id)]["owner"] == {
        "id": str(seeded_docs["other_user"].id),
        "display_name": "其他用户",
    }
    assert by_id[str(seeded_docs["system"].id)]["owner"] is None


# ------------------------------------------------------------------ 能力开关
async def test_knowledge_disabled_returns_403_on_every_endpoint(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    fake_storage: Any,
    fake_embedding: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = uuid.uuid4()
    monkeypatch.setenv("KNOWLEDGE_ENABLED", "false")
    reset_settings_cache()
    try:
        responses = await asyncio.gather(
            _upload(client, auth, filename="disabled.md", content=b"# x\n\ny"),
            client.get(DOCUMENTS_URL, headers=auth),
            client.get(f"{DOCUMENTS_URL}/{document_id}", headers=auth),
            client.delete(f"{DOCUMENTS_URL}/{document_id}", headers=auth),
            client.post(f"{DOCUMENTS_URL}/{document_id}/retry", headers=auth),
            client.post(SEARCH_URL, json={"query": "退货"}, headers=auth),
            client.post(REBUILD_URL, json={"scope": "all"}, headers=auth),
        )
    finally:
        reset_settings_cache()

    assert len(responses) == 7
    for response in responses:
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "capability_disabled"


# ------------------------------------------------------------------ 检索
async def test_search_embedding_unavailable_returns_503(
    client: httpx.AsyncClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.modules.knowledge.service.get_embedding_provider",
        lambda: _BrokenEmbeddingProvider(),
    )

    response = await client.post(SEARCH_URL, json={"query": "退货"}, headers=auth)

    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "service_unavailable"
    assert "Embedding" in error["message"]


async def test_search_hit_contract_and_field_mapping(
    client: httpx.AsyncClient, auth: dict[str, str], seeded_docs: dict
) -> None:
    response = await client.post(SEARCH_URL, json={"query": "保留吊牌", "top_k": 5}, headers=auth)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"hits", "elapsed_ms"}
    assert isinstance(payload["elapsed_ms"], int)
    assert payload["elapsed_ms"] >= 0
    assert payload["hits"]

    for hit in payload["hits"]:
        assert set(hit) == HIT_KEYS
        assert 0.0 <= hit["score"] <= 1.0

    top = payload["hits"][0]
    assert top["document_id"] == str(seeded_docs["system"].id)
    # title 取分块章节标题（heading），document_title 取文档标题。
    assert top["title"] == "吊牌要求"
    assert top["document_title"] == "退货政策"
    assert top["document_filename"] == "return-policy.md"
    assert top["category"] == "售后"


async def test_search_category_filter(
    client: httpx.AsyncClient, auth: dict[str, str], seeded_docs: dict
) -> None:
    response = await client.post(
        SEARCH_URL,
        json={"query": "发货", "top_k": 5, "filters": {"category": "物流"}},
        headers=auth,
    )

    assert response.status_code == 200, response.text
    hits = response.json()["hits"]
    assert hits
    assert all(hit["category"] == "物流" for hit in hits)


# ------------------------------------------------------------------ 重建
async def test_rebuild_returns_admin_stats_contract(
    client: httpx.AsyncClient, admin_auth: dict[str, str], seeded_docs: dict
) -> None:
    response = await client.post(REBUILD_URL, json={"scope": "all"}, headers=admin_auth)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"documents", "chunks", "failed", "elapsed_ms"}
    assert payload["documents"] == 3
    assert payload["failed"] == 0
    assert payload["chunks"] == sum(document.chunk_count for document in seeded_docs["documents"])
    assert isinstance(payload["elapsed_ms"], int)
    assert payload["elapsed_ms"] >= 0


async def test_rebuild_scope_own_means_all_users_private_documents(
    client: httpx.AsyncClient, admin_auth: dict[str, str], seeded_docs: dict
) -> None:
    # R9：own 指「所有用户的私有文档」，不是管理员本人的文档（管理员自己没有文档）。
    response = await client.post(REBUILD_URL, json={"scope": "own"}, headers=admin_auth)

    assert response.status_code == 200, response.text
    assert response.json()["documents"] == 2


async def test_openapi_documents_rebuild_rulings(client: httpx.AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    operation = schema["paths"]["/api/v1/knowledge/rebuild"]["post"]

    assert "/api/v1/knowledge/documents" in schema["paths"]
    assert "/api/v1/knowledge/documents/{document_id}" in schema["paths"]
    assert "/api/v1/knowledge/documents/{document_id}/retry" in schema["paths"]
    assert "/api/v1/knowledge/search" in schema["paths"]
    description = operation["description"]
    # R9 / R10 必须在 OpenAPI 描述里写清楚。
    assert "所有用户的私有文档" in description
    assert "documents" in description
    assert "failed" in description
    assert "chunks" in description
    # 503 是知识库检索的真实错误码，必须进 OpenAPI 响应声明。
    assert "503" in operation["responses"]
