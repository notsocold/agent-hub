"""M4 向量检索层测试：字段契约、相似度排序、过滤与可见性。

本文件自带本地夹具（按 R8 不改 conftest）：文档/分块直接用 ORM 写入，
向量用字面量单位向量，使余弦距离有精确可断言的期望值；另有一个用例
用 :class:`FakeEmbeddingProvider` 验证 Provider 输出可直接作为检索入参。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import fields
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.db.models.user import User
from app.db.session import get_session_factory
from app.db.vector import SearchHit, search_chunks
from app.modules.knowledge.embedding import FakeEmbeddingProvider

# 期望的 SearchHit 字段顺序（Task 9 按名字映射到 API 响应）。
SEARCH_HIT_FIELDS = [
    "chunk_id",
    "document_id",
    "chunk_index",
    "content",
    "heading",
    "document_title",
    "document_filename",
    "category",
    "updated_at",
    "score",
]

WARRANTY_CONTENT = "退货需要保留吊牌与完整包装。"
WARRANTY_FLOW_CONTENT = "退货流程：提交申请后三日内寄回。"
SHIPPING_CONTENT = "现货商品 24 小时内发货。"
BOB_CONTENT = "Bob 的私有退货备注。"
PENDING_CONTENT = "尚未完成索引的正文。"
DIAGONAL_CONTENT = "夹角 45 度的片段。"

WARRANTY_HEADING = "退货政策 > 吊牌要求"


def _vector(*values: float) -> list[float]:
    """构造前几维取值的向量；其余维度补零（余弦距离不依赖模长）。"""
    vector = [0.0] * get_settings().embedding_dim
    for index, value in enumerate(values):
        vector[index] = value
    return vector


async def _create_user(session: AsyncSession, *, prefix: str) -> User:
    user = User(email=f"{prefix}-{uuid.uuid4().hex[:8]}@example.com", password_hash="x")
    session.add(user)
    await session.flush()
    return user


def _add_document(
    session: AsyncSession,
    *,
    owner: User | None,
    title: str,
    filename: str,
    category: str,
    status: str = "ready",
    chunks: list[tuple[str, list[float], dict[str, Any]]],
) -> KnowledgeDocument:
    """新建文档并挂上分块；chunk_index 按列表顺序编号。"""
    document = KnowledgeDocument(
        owner_user_id=owner.id if owner is not None else None,
        title=title,
        filename=filename,
        category=category,
        status=status,
        checksum=hashlib.sha256(uuid.uuid4().hex.encode("utf-8")).hexdigest(),
        size_bytes=42,
        storage_key=f"knowledge/{uuid.uuid4().hex}.md",
        metadata_={},
    )
    for index, (content, embedding, metadata) in enumerate(chunks):
        document.chunks.append(
            KnowledgeChunk(
                chunk_index=index,
                content=content,
                embedding=embedding,
                token_count=len(content),
                metadata_=metadata,
            )
        )
    session.add(document)
    return document


@pytest.fixture
async def seeded_docs(db: None) -> dict[str, Any]:
    """一套覆盖「系统/本人/他人/未就绪/分类/heading 回退」的分块数据。"""
    query_vector = _vector(1.0)
    async with get_session_factory()() as session:
        alice = await _create_user(session, prefix="alice")
        bob = await _create_user(session, prefix="bob")

        warranty = _add_document(
            session,
            owner=None,
            title="退货政策",
            filename="return-policy.md",
            category="售后",
            chunks=[
                (WARRANTY_CONTENT, _vector(1.0), {"heading": WARRANTY_HEADING}),
                # [3, 4] 与 query 的余弦 = 0.6 → score 0.6（score 即余弦相似度）
                (WARRANTY_FLOW_CONTENT, _vector(3.0, 4.0), {}),
            ],
        )
        shipping = _add_document(
            session,
            owner=alice,
            title="发货时效",
            filename="shipping.md",
            category="物流",
            # 与 query 正交 → score 0.0
            # heading 为空串也要回退为文档标题
            chunks=[(SHIPPING_CONTENT, _vector(0.0, 1.0), {"heading": ""})],
        )
        bob_private = _add_document(
            session,
            owner=bob,
            title="Bob 私有文档",
            filename="bob.md",
            category="售后",
            chunks=[(BOB_CONTENT, _vector(1.0), {"heading": "Bob 私有"})],
        )
        pending = _add_document(
            session,
            owner=None,
            title="未完成文档",
            filename="pending.md",
            category="售后",
            status="pending",
            chunks=[(PENDING_CONTENT, _vector(1.0), {"heading": "未完成"})],
        )
        diagonal = _add_document(
            session,
            owner=alice,
            title="对角向量",
            filename="diagonal.md",
            category="售后",
            # [1, 1] 与 query 的余弦 = 1/√2 → score = 0.7071（验证 4 位小数舍入）
            chunks=[(DIAGONAL_CONTENT, _vector(1.0, 1.0), {"heading": "对角章节"})],
        )
        await session.commit()
        return {
            "query_vector": query_vector,
            "alice_id": alice.id,
            "bob_id": bob.id,
            "warranty_id": warranty.id,
            "warranty_chunk_ids": [chunk.id for chunk in warranty.chunks],
            "warranty_updated_at": warranty.updated_at,
            "shipping_id": shipping.id,
            "shipping_chunk_id": shipping.chunks[0].id,
            "bob_private_id": bob_private.id,
            "pending_id": pending.id,
            "diagonal_id": diagonal.id,
        }


async def _search(
    session: AsyncSession, seeded: dict[str, Any], **overrides: Any
) -> list[SearchHit]:
    """按常见默认参数检索，仅在用例显式覆盖时改动。"""
    kwargs: dict[str, Any] = {
        "embedding": seeded["query_vector"],
        "user_id": seeded["alice_id"],
        "top_k": 10,
        "threshold": 0.0,
        "categories": None,
    }
    kwargs.update(overrides)
    return await search_chunks(session, **kwargs)


def test_search_hit_dataclass_fields_match_contract() -> None:
    assert [field.name for field in fields(SearchHit)] == SEARCH_HIT_FIELDS


async def test_orders_by_similarity_and_populates_all_fields(
    db: None, seeded_docs: dict[str, Any]
) -> None:
    async with get_session_factory()() as session:
        hits = await _search(session, seeded_docs)

    # 由近到远：完全相同(1.0) → 余弦 1/√2(0.7071) → 余弦 0.6(0.6) → 正交(0.0)
    assert [hit.score for hit in hits] == [1.0, 0.7071, 0.6, 0.0]
    assert [hit.document_id for hit in hits] == [
        seeded_docs["warranty_id"],
        seeded_docs["diagonal_id"],
        seeded_docs["warranty_id"],
        seeded_docs["shipping_id"],
    ]

    top = hits[0]
    assert top.chunk_id == seeded_docs["warranty_chunk_ids"][0]
    assert top.document_id == seeded_docs["warranty_id"]
    assert top.chunk_index == 0
    assert top.content == WARRANTY_CONTENT
    assert top.heading == WARRANTY_HEADING
    assert top.document_title == "退货政策"
    assert top.document_filename == "return-policy.md"
    assert top.category == "售后"
    assert top.updated_at == seeded_docs["warranty_updated_at"]
    assert top.score == 1.0


async def test_heading_falls_back_to_document_title(db: None, seeded_docs: dict[str, Any]) -> None:
    async with get_session_factory()() as session:
        hits = await _search(session, seeded_docs)
    by_chunk = {hit.chunk_id: hit for hit in hits}

    assert by_chunk[seeded_docs["warranty_chunk_ids"][0]].heading == WARRANTY_HEADING
    # 第二个分块没有 metadata.heading，回退为文档标题
    assert by_chunk[seeded_docs["warranty_chunk_ids"][1]].heading == "退货政策"
    # 空串同样视为缺失
    assert by_chunk[seeded_docs["shipping_chunk_id"]].heading == "发货时效"


async def test_category_filter(db: None, seeded_docs: dict[str, Any]) -> None:
    async with get_session_factory()() as session:
        hits = await _search(session, seeded_docs, categories=["售后"])

    assert hits
    assert {hit.category for hit in hits} <= {"售后"}
    # 只剩系统文档 + Alice 自己的文档；物流分类被过滤掉
    assert {hit.document_id for hit in hits} == {
        seeded_docs["warranty_id"],
        seeded_docs["diagonal_id"],
    }


async def test_excludes_other_users_documents(db: None, seeded_docs: dict[str, Any]) -> None:
    async with get_session_factory()() as session:
        alice_hits = await _search(session, seeded_docs)
        bob_hits = await _search(session, seeded_docs, user_id=seeded_docs["bob_id"])

    alice_document_ids = {hit.document_id for hit in alice_hits}
    # Alice 看不到 Bob 的私有文档，「自己的 + 系统的」是硬边界
    assert seeded_docs["bob_private_id"] not in alice_document_ids
    assert seeded_docs["warranty_id"] in alice_document_ids

    bob_document_ids = {hit.document_id for hit in bob_hits}
    assert seeded_docs["bob_private_id"] in bob_document_ids
    assert seeded_docs["warranty_id"] in bob_document_ids
    # Bob 也看不到 Alice 的私有文档
    assert seeded_docs["shipping_id"] not in bob_document_ids
    assert seeded_docs["diagonal_id"] not in bob_document_ids


async def test_admin_does_not_get_wider_scope(db: None, seeded_docs: dict[str, Any]) -> None:
    """管理员检索时也只看系统文档 + 自己的文档，不混入他人私有资料。"""
    async with get_session_factory()() as session:
        admin = await _create_user(session, prefix="admin")
        admin.role = "admin"
        await session.commit()
        admin_id = admin.id

    async with get_session_factory()() as session:
        hits = await _search(session, seeded_docs, user_id=admin_id)

    document_ids = {hit.document_id for hit in hits}
    assert seeded_docs["warranty_id"] in document_ids
    assert seeded_docs["bob_private_id"] not in document_ids


async def test_only_ready_documents_are_searchable(db: None, seeded_docs: dict[str, Any]) -> None:
    async with get_session_factory()() as session:
        hits = await _search(session, seeded_docs)

    # 该分块的向量与 query 完全相同（本会排第一），但文档未就绪，必须排除。
    assert seeded_docs["pending_id"] not in {hit.document_id for hit in hits}


async def test_threshold_filters_low_scores(db: None, seeded_docs: dict[str, Any]) -> None:
    async with get_session_factory()() as session:
        hits = await _search(session, seeded_docs, threshold=0.65)

    assert [hit.score for hit in hits] == [1.0, 0.7071]
    assert all(hit.score >= 0.65 for hit in hits)


async def test_top_k_limits_results(db: None, seeded_docs: dict[str, Any]) -> None:
    async with get_session_factory()() as session:
        hits = await _search(session, seeded_docs, top_k=2)

    assert len(hits) == 2
    assert [hit.score for hit in hits] == [1.0, 0.7071]
    assert [hit.document_id for hit in hits] == [
        seeded_docs["warranty_id"],
        seeded_docs["diagonal_id"],
    ]


async def test_empty_database_returns_empty_list(db: None) -> None:
    async with get_session_factory()() as session:
        hits = await search_chunks(
            session,
            embedding=_vector(1.0),
            user_id=uuid.uuid4(),
            top_k=5,
            threshold=0.0,
            categories=None,
        )

    assert hits == []


async def test_unmatched_category_returns_empty_list(db: None, seeded_docs: dict[str, Any]) -> None:
    async with get_session_factory()() as session:
        hits = await _search(session, seeded_docs, categories=["不存在的分类"])

    assert hits == []


async def test_fake_embedding_provider_ranks_relevant_chunk_first(db: None) -> None:
    """真实 embedding 提供方的输出可直接作为检索入参。"""
    provider = FakeEmbeddingProvider(get_settings().embedding_dim)
    relevant = WARRANTY_CONTENT
    irrelevant = "本季度财务报表已归档。"
    chunk_vectors = await provider.embed([relevant, irrelevant])
    query_vector = (await provider.embed(["退货吊牌要求"]))[0]

    async with get_session_factory()() as session:
        document = _add_document(
            session,
            owner=None,
            title="退货政策",
            filename="return-policy.md",
            category="售后",
            chunks=[
                (relevant, chunk_vectors[0], {"heading": WARRANTY_HEADING}),
                (irrelevant, chunk_vectors[1], {"heading": "财务"}),
            ],
        )
        await session.commit()
        document_id = document.id

    async with get_session_factory()() as session:
        hits = await search_chunks(
            session,
            embedding=query_vector,
            user_id=uuid.uuid4(),
            top_k=5,
            threshold=0.0,
            categories=None,
        )

    assert hits
    assert hits[0].document_id == document_id
    assert hits[0].content == relevant
