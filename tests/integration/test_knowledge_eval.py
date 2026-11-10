"""M4 知识库离线评测集：链路组 CI 门禁 + 语义组真 key 才可验证。

设计文档 §11.2 与反目标 §2.3：

- **链路组**：问题复用目标文档正文里的原词，假向量（bag-of-bigrams）也能正确排序，
  因此作为零网络 CI 门禁——断言 top-1 命中文档且命中片段包含 keyword，**不看分数与阈值**
  （spec §8.2 明确 fake 分数不可解释）；
- **语义组**：同义改写，问题与目标文档不共享任何 bigram，fake 下必然排不上来，
  只有真实 Embedding 才能召回；没有可用的真实 key 时 skip 并在报告里如实说明，
  绝不把「跳过」当成「通过」，也不把 fake 的召回率当成语义能力证据；
- 所有断言都只看「top-k 命中预期文档」，不依赖分数与阈值。

手动跑语义组（使用真实 Embedding，会退出零网络基线）：

    KNOWLEDGE_EVAL_REAL=1 TEST_DATABASE_URL=postgresql+psycopg://devuser:devpass@localhost:54321/agent_hub_test \\
      EMBEDDING_PROVIDER=openai EMBEDDING_API_KEY=<真实 key> EMBEDDING_BASE_URL=<真实地址> \\
      .venv/bin/python -m pytest tests/integration/test_knowledge_eval.py -k semantic -v

``KNOWLEDGE_EVAL_REAL=1`` 由 ``tests/conftest.py`` 识别：置 1 后 conftest 不再强制
fake provider/空 key，而是让 .env 或 shell 中的真实 Embedding 配置生效；不设置时
仍保持「强制假向量、零网络」的默认基线。
"""

from __future__ import annotations

import json
import math
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models.knowledge import KnowledgeDocument
from app.db.models.user import User
from app.modules.knowledge.embedding import FakeEmbeddingProvider, get_embedding_provider
from app.modules.knowledge.service import ingest_bytes, search

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
DOCS_DIR = FIXTURES_DIR / "knowledge_docs"
EVAL_FILE = FIXTURES_DIR / "knowledge_eval.jsonl"
STORAGE_ROOT = Path("data/test-knowledge")

CHAIN_GROUP = "chain"
SEMANTIC_GROUP = "semantic"
GROUPS = frozenset({CHAIN_GROUP, SEMANTIC_GROUP})

# 评测文档分类：售后/产品/安全/计费/运维各两篇，保证「分类过滤」也有数据可评。
DOC_CATEGORIES: dict[str, str] = {
    "after-sales-return-policy.md": "售后",
    "after-sales-repair.md": "售后",
    "product-model-specs.md": "产品",
    "product-release-process.md": "产品",
    "security-incident-response.md": "安全",
    "security-password-policy.md": "安全",
    "billing-invoice.md": "计费",
    "billing-subscription.md": "计费",
    "ops-backup-restore.md": "运维",
    "ops-oncall.md": "运维",
}

# 与 app/modules/knowledge/embedding.py 的 fake 分词保持一致：中文 bigram + 英文小写词。
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_LATIN_RUN = re.compile(r"[a-z0-9]+")
# Markdown ATX 标题行：正文校验要排除标题，因为命中片段的 content 不含标题行。
_HEADING_LINE = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")


# --------------------------------------------------------------------- 数据读取
def _fake_tokens(text: str) -> set[str]:
    """按 fake provider 的口径取 token 集合，用于校验语义组是否「真语义」。"""
    tokens: set[str] = set()
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    tokens.update(_LATIN_RUN.findall(text.lower()))
    return tokens


def _load_cases() -> list[dict[str, Any]]:
    """读取评测用例；文件缺失直接报错，不静默跳过。"""
    cases: list[dict[str, Any]] = []
    with EVAL_FILE.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as exc:  # pragma: no cover - 数据错误时给出定位
                raise AssertionError(
                    f"{EVAL_FILE.name} 第 {line_number} 行不是合法 JSON：{exc}"
                ) from exc
    return cases


def _document_text(filename: str) -> str:
    return (DOCS_DIR / filename).read_text(encoding="utf-8")


def _body_text(text: str) -> str:
    """去掉 Markdown 标题行后的正文；keyword 必须出现在正文里才能进命中片段。"""
    return "\n".join(line for line in text.splitlines() if not _HEADING_LINE.match(line))


def _document_title(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


# ------------------------------------------------------------- 真实 key 可用性判断
def _real_embedding_skip_reason() -> str | None:
    """没有可用真实 Embedding 时返回中文原因，供语义组 pytest.skip 使用。

    判定与设计文档 §8.1/§8.2 对齐：显式 fake、密钥不可用、或实际 provider 回退到
    fake 时，语义能力都不可验证，必须跳过而不是拿 fake 的排序充当召回证据。
    """
    settings = get_settings()
    if settings.embedding_provider == "fake":
        return (
            "语义组需要真实 Embedding：当前 EMBEDDING_PROVIDER=fake"
            "（测试环境强制假向量、零网络），跳过不代表语义召回能力"
        )
    if not settings.embedding_key_usable:
        return (
            "语义组需要真实 Embedding：EMBEDDING_API_KEY 未配置或为占位符，跳过不代表语义召回能力"
        )
    try:
        provider = get_embedding_provider()
    except Exception as exc:  # 初始化失败也要如实跳过并给出原因
        return f"语义组需要真实 Embedding：初始化提供方失败（{exc}）"
    if isinstance(provider, FakeEmbeddingProvider):
        return "语义组需要真实 Embedding：当前检索提供方回退为 fake，跳过不代表语义召回能力"
    return None


_SKIP_REASON = _real_embedding_skip_reason()


# --------------------------------------------------------------------- 文档入库夹具
async def _ingest_fixture_documents(db: AsyncSession, user: User) -> dict[str, KnowledgeDocument]:
    """把 ``tests/fixtures/knowledge_docs/`` 全部文档以系统文档身份入库。

    使用真实数据库行、真实本地存储与真实切分；向量取决于当前 provider
    （链路组由 ``fake_embedding`` 固定为假向量，语义组用真实配置）。
    """
    documents: dict[str, KnowledgeDocument] = {}
    for filename in sorted(DOC_CATEGORIES):
        path = DOCS_DIR / filename
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        document = await ingest_bytes(
            db,
            owner=None,
            title=_document_title(text),
            filename=filename,
            category=DOC_CATEGORIES[filename],
            raw=raw,
        )
        assert document.status == "ready", (
            f"评测文档 {filename} 入库失败（status={document.status}）：{document.error}"
        )
        documents[filename] = document
    assert documents, "评测文档目录为空，无法执行离线评测"
    return documents


@pytest.fixture
async def seeded_fixture_docs(
    db: AsyncSession,
    user: User,
    fake_storage: Any,
    fake_embedding: Any,
) -> Any:
    """链路组夹具：fake embedding 入库全部评测文档，结束后清理测试存储目录。"""
    documents = await _ingest_fixture_documents(db, user)
    yield {"documents": documents, "cases": _load_cases()}
    shutil.rmtree(STORAGE_ROOT, ignore_errors=True)


@pytest.fixture
async def real_embedding_seeded_docs(
    db: AsyncSession,
    user: User,
    fake_storage: Any,
) -> Any:
    """语义组夹具：用当前配置的真实 embedding 入库，仅在真实 key 可用时启用。"""
    documents = await _ingest_fixture_documents(db, user)
    yield {"documents": documents, "cases": _load_cases()}
    shutil.rmtree(STORAGE_ROOT, ignore_errors=True)


# ------------------------------------------------------------------- 链路组（CI 门禁）
async def test_chain_group_top1_hits(
    db: AsyncSession, user: User, seeded_fixture_docs: dict[str, Any]
) -> None:
    """15 条链路组用例：每条都必须 top-1 命中预期文档且片段含 keyword。

    失败信息收集后统一断言，避免第一条失败就掩盖后面的问题。
    """
    cases = [case for case in seeded_fixture_docs["cases"] if case["group"] == CHAIN_GROUP]
    assert len(cases) == 15, f"链路组用例数应为 15，实际为 {len(cases)}"

    failures: list[str] = []
    for case in cases:
        hits = await search(db, user, query=case["question"], top_k=3, categories=None)
        if not hits:
            failures.append(f"{case['id']}：检索结果为空（问题：{case['question']}）")
            continue
        top = hits[0]
        if top.document_filename != case["doc"]:
            failures.append(
                f"{case['id']}：top-1 命中文档 {top.document_filename}，"
                f"期望 {case['doc']}（问题：{case['question']}）"
            )
            continue
        if case["keyword"] not in top.content:
            failures.append(
                f"{case['id']}：top-1 命中片段缺少关键词「{case['keyword']}」"
                f"（问题：{case['question']}）"
            )

    assert not failures, f"链路组 top-1 命中失败 {len(failures)}/{len(cases)} 条：\n" + "\n".join(
        failures
    )


# --------------------------------------------------------------- 语义组（真 key 才跑）
@pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")
async def test_semantic_group_requires_real_embedding(
    db: AsyncSession, user: User, real_embedding_seeded_docs: dict[str, Any]
) -> None:
    """5 条语义组用例：真实 Embedding 下 top-3 召回率必须 ≥ 90%。

    这是「语义能力」唯一有效的证据；没有真实 key 时本用例整体 skip，
    由 :func:`test_semantic_group_is_not_solvable_by_fake_embeddings` 证明跳过是诚实的。
    """
    cases = [
        case for case in real_embedding_seeded_docs["cases"] if case["group"] == SEMANTIC_GROUP
    ]
    assert len(cases) == 5, f"语义组用例数应为 5，实际为 {len(cases)}"

    hit_cases: list[str] = []
    failures: list[str] = []
    for case in cases:
        hits = await search(db, user, query=case["question"], top_k=3, categories=None)
        matched = any(
            hit.document_filename == case["doc"] and case["keyword"] in hit.content for hit in hits
        )
        if matched:
            hit_cases.append(case["id"])
        else:
            top_docs = [hit.document_filename for hit in hits]
            failures.append(
                f"{case['id']}：top-3 未命中 {case['doc']}（问题：{case['question']}；"
                f"实际命中文档：{top_docs}）"
            )

    required = math.ceil(len(cases) * 0.9)
    recall = len(hit_cases) / len(cases)
    assert len(hit_cases) >= required, (
        f"语义组 top-3 召回率 {recall:.0%}（{len(hit_cases)}/{len(cases)}），"
        f"低于 90%（至少需要 {required} 条）：\n" + "\n".join(failures)
    )


async def test_semantic_group_is_not_solvable_by_fake_embeddings(
    db: AsyncSession, user: User, seeded_fixture_docs: dict[str, Any]
) -> None:
    """诚实性守卫：语义组在 fake embedding 下必须无法命中目标文档。

    语义组问题与目标文档不共享 token，fake 的词袋模型拿不到任何有效信号；
    如果这里出现命中，说明用例其实仍依赖链路词面重叠，必须重写数据。
    """
    cases = [case for case in seeded_fixture_docs["cases"] if case["group"] == SEMANTIC_GROUP]
    assert len(cases) == 5, f"语义组用例数应为 5，实际为 {len(cases)}"

    incorrectly_hit: list[str] = []
    for case in cases:
        hits = await search(db, user, query=case["question"], top_k=3, categories=None)
        if any(hit.document_filename == case["doc"] for hit in hits):
            incorrectly_hit.append(f"{case['id']}（问题：{case['question']} 竟命中 {case['doc']}）")

    assert not incorrectly_hit, (
        "语义组在 fake embedding 下本应全部未命中，以下用例却命中了目标文档，"
        "说明它们仍是词面链路而不是语义：" + "；".join(incorrectly_hit)
    )


# ------------------------------------------------------------------------ 数据守卫
def test_fixture_data_is_well_formed() -> None:
    """离线评测数据自检：文档数量/长度/分类、用例数量/字段、keyword 与 id 唯一性。"""
    documents = sorted(DOCS_DIR.glob("*.md"))
    assert len(documents) >= 8, f"评测文档至少 8 篇，实际 {len(documents)} 篇"
    filenames = {path.name for path in documents}
    assert set(DOC_CATEGORIES) == filenames, (
        "DOC_CATEGORIES 必须与实际文档一一对应；"
        f"缺少分类：{sorted(filenames - set(DOC_CATEGORIES))}；"
        f"多余分类：{sorted(set(DOC_CATEGORIES) - filenames)}"
    )

    # 每篇 300–800 字符，且正文里必须含 keyword 可落地的具体事实。
    for path in documents:
        text = path.read_text(encoding="utf-8")
        assert 300 <= len(text) <= 800, (
            f"文档 {path.name} 长度 {len(text)} 字符，超出 300–800 的要求"
        )
        assert text.startswith("# "), f"文档 {path.name} 缺少一级标题"
        assert text.count("\n## ") >= 1, f"文档 {path.name} 缺少章节标题"

    # 至少两篇文档同分类，分类过滤才可被评测覆盖。
    category_counts: dict[str, int] = {}
    for category in DOC_CATEGORIES.values():
        category_counts[category] = category_counts.get(category, 0) + 1
    assert max(category_counts.values()) >= 2, (
        f"至少两篇文档应共享同一 category，实际分布：{category_counts}"
    )

    cases = _load_cases()
    assert len(cases) == 20, f"评测用例应为 20 条，实际 {len(cases)} 条"
    required_fields = {"id", "question", "doc", "keyword", "group"}
    seen_ids: set[str] = set()
    group_counts: dict[str, int] = {}
    covered_docs: set[str] = set()
    chain_cases = 0
    for case in cases:
        missing = required_fields - case.keys()
        assert not missing, f"用例 {case.get('id')} 缺少字段：{sorted(missing)}"
        case_id = case["id"]
        assert case_id and case_id not in seen_ids, f"用例 id 为空或重复：{case_id}"
        seen_ids.add(case_id)
        assert case["group"] in GROUPS, (
            f"用例 {case_id} 的 group={case['group']!r} 非法，只允许 {sorted(GROUPS)}"
        )
        group_counts[case["group"]] = group_counts.get(case["group"], 0) + 1
        assert case["doc"] in filenames, f"用例 {case_id} 指向不存在的文档：{case['doc']}"
        covered_docs.add(case["doc"])
        text = _document_text(case["doc"])
        body = _body_text(text)
        assert case["keyword"] in body, (
            f"用例 {case_id} 的 keyword「{case['keyword']}」未出现在 {case['doc']} 正文中"
        )
        if case["group"] == CHAIN_GROUP:
            chain_cases += 1
            shared = _fake_tokens(case["question"]) & _fake_tokens(text)
            assert len(shared) >= 3, (
                f"链路组 {case_id} 与目标文档只共享 {len(shared)} 个 token，"
                "fake 下无法稳定排序，请让问题复用正文原词"
            )
        else:
            shared = _fake_tokens(case["question"]) & _fake_tokens(text)
            assert not shared, (
                f"语义组 {case_id} 与目标文档共享 token {sorted(shared)}，"
                "fake 词袋也能命中，不能用来证明语义能力"
            )

    assert group_counts == {CHAIN_GROUP: 15, SEMANTIC_GROUP: 5}, (
        f"用例分组应为链路组 15 / 语义组 5，实际 {group_counts}"
    )
    assert chain_cases == 15
    assert covered_docs == filenames, (
        f"有文档没有被评测用例覆盖：{sorted(filenames - covered_docs)}"
    )
