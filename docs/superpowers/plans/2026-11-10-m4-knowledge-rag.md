# M4 知识库 RAG 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 agent-hub 的主智能体能检索内部文档并在回答里给出可点击、可落库的来源引用，同时提供完整的文档入库与管理 API。

**Architecture:** 新增 `app/modules/knowledge/`，按「解析 → 切分 → 向量化 → 落库」单向流水线组织；向量 SQL 隔离在 `app/db/vector/`；原文通过新增的 `app/core/storage.py`（local/S3 双后端）保存，供 retry/rebuild 重放；聊天侧用 `search_knowledge` 工具接入，引用经 `RunContext.citations` 汇总后同时写入 SSE 与 `messages.citations`。

**Tech Stack:** FastAPI、SQLAlchemy 2.x async、Alembic、PostgreSQL 18 + pgvector 0.8.7、langchain `StructuredTool`、httpx、boto3、pypdf、pytest-asyncio。

**Spec:** `docs/superpowers/specs/2026-11-10-m4-knowledge-rag-design.md`

## Global Constraints

- Python `>=3.13`；ruff `line-length = 100`、`target-version = "py313"`。
- 注释与用户可见错误消息一律中文；错误走 `app/core/errors.py` 既有类，不自造 code。
- 测试零网络、零外部存储：conftest 强制 `EMBEDDING_PROVIDER=fake`、`EMBEDDING_API_KEY=""`、`STORAGE_BACKEND=local`。
- 不启动任何新容器；`dev_postgres` 已有 `vector` 扩展，`dev_minio` 仅用于可选的真实联调。
- 数据库字段、API 响应字段以 spec §5、§7 为准，与前端 `KnowledgeDocument` / `KnowledgeSearchHit` / `Citation` 逐字段一致。
- 每个任务结束时 `pytest -q` 必须全绿（基线 108 + 新增）。

## Review Focus

1. **异形上传**：空文件、>10MB、大写扩展名（`.MD`）、二进制内容改名 `.txt`、无文本层 PDF —— 期望是 4xx 或 `status=failed` 加可读 `error`，绝不 500。
2. **并发**：同归属同 checksum 并发上传、两个管理员同时 rebuild —— 期望唯一索引兜底 + 幂等返回，不留半截分块。
3. **越权**：普通用户访问他人文档、管理员 `owner_scope=all` 以外的检索范围、跨用户 retry/delete —— 期望 404/403，且**管理员检索永不混入他人私有文档**。
4. **降级**：假 embedding 阈值 0、embedding 真实 provider 失败、对象存储不可用、知识库为空 —— 期望空结果或 503/`failed`，不 500、不静默返回错数据。
5. **引用一致性**：多次检索去重后 `index` 连续从 1 开始；rebuild 后旧 `chunk_id` 失效但 `Citation` 快照仍可展示。

---

### Task 1: 依赖与配置项

**Files:**
- Modify: `pyproject.toml`（dependencies 增加 `pypdf`、`boto3`）
- Modify: `app/core/config.py`
- Modify: `.env.example`、`.env`
- Test: `tests/test_config_knowledge_unit.py`

**Interfaces:**
- Produces: `Settings` 新字段 —— `knowledge_enabled: bool = True`、`knowledge_dir: Path = Path("data/knowledge")`、`knowledge_max_upload_bytes: int = 10 * 1024 * 1024`、`knowledge_chunk_size: int = 800`、`knowledge_chunk_overlap: int = 80`、`knowledge_max_chunks_per_document: int = 5000`、`knowledge_max_pdf_pages: int = 500`、`storage_backend: Literal["local", "s3"] = "local"`、`s3_endpoint_url: str = ""`、`s3_bucket: str = ""`、`s3_access_key: str = ""`、`s3_secret_key: str = ""`、`s3_region: str = "us-east-1"`、`s3_prefix: str = "knowledge/"`、`embedding_provider: Literal["auto", "fake", "openai"] = "auto"`、`embedding_timeout_seconds: int = 30`、`embedding_batch_size: int = 32`
- Produces: `Settings.embedding_key_usable` property（bool）与模块函数 `is_placeholder_api_key(key: str) -> bool`
- Consumes: 无

- [ ] **Step 1: 写失败测试**

```python
# tests/test_config_knowledge_unit.py
from app.core.config import Settings, is_placeholder_api_key

def test_placeholder_keys_detected():
    assert is_placeholder_api_key("sk-xxx")
    assert is_placeholder_api_key("")
    assert not is_placeholder_api_key("sk-" + "a" * 40)

def test_knowledge_defaults(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-xxx")
    s = Settings(database_url="postgresql+psycopg://u:p@localhost/x")
    assert s.knowledge_enabled is True
    assert s.knowledge_chunk_size == 800
    assert s.knowledge_chunk_overlap == 80
    assert s.storage_backend == "local"
    assert s.embedding_provider == "auto"
    assert s.embedding_key_usable is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_config_knowledge_unit.py -v`
Expected: FAIL —— `ImportError: cannot import name 'is_placeholder_api_key'`

- [ ] **Step 3: 实现配置**

`app/core/config.py`：在「Embedding / 检索」段追加 `embedding_provider`、`embedding_timeout_seconds`、`embedding_batch_size`；新增「知识库 / 存储」段放 `knowledge_*` 与 `s3_*`；新增模块级正则

```python
_PLACEHOLDER_KEY = re.compile(r"^sk-(x+|your.*|test.*|placeholder.*)$", re.IGNORECASE)
def is_placeholder_api_key(key: str) -> bool:
    return len(key.strip()) < 20 or bool(_PLACEHOLDER_KEY.match(key.strip()))
```

`embedding_key_usable` 返回 `not is_placeholder_api_key(self.embedding_api_key)`。
`.env.example` 与 `.env`：`EMBEDDING_API_KEY=`（清空占位符），并补 `KNOWLEDGE_ENABLED/KNOWLEDGE_DIR/KNOWLEDGE_CHUNK_SIZE/KNOWLEDGE_CHUNK_OVERLAP/KNOWLEDGE_MAX_UPLOAD_BYTES/KNOWLEDGE_MAX_CHUNKS_PER_DOCUMENT/KNOWLEDGE_MAX_PDF_PAGES/STORAGE_BACKEND/S3_*/EMBEDDING_PROVIDER/EMBEDDING_TIMEOUT_SECONDS/EMBEDDING_BATCH_SIZE`。

- [ ] **Step 4: 安装依赖并跑测试**

Run: `uv sync && pytest tests/test_config_knowledge_unit.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add pyproject.toml uv.lock app/core/config.py .env.example tests/test_config_knowledge_unit.py
git commit -m "feat: 知识库与存储相关配置项"
```

---

### Task 2: 存储抽象（local / S3）

**Files:**
- Create: `app/core/storage.py`
- Test: `tests/test_storage_unit.py`

**Interfaces:**
- Produces:
  - `class FileStorage(Protocol)`: `async put(key: str, data: bytes, *, content_type: str = "") -> None`、`async get(key: str) -> bytes`、`async delete(key: str) -> None`、`async exists(key: str) -> bool`
  - `class LocalFileStorage(root: Path)`、`class S3FileStorage(client: Any, bucket: str, prefix: str = "")`
  - `get_storage(settings: Settings | None = None) -> FileStorage`（进程级缓存）
  - `reset_storage() -> None`（测试用）
  - `ObjectNotFoundError(Exception)`（供 service 判「原始文件缺失」）
- Consumes: Task 1 的 `Settings.storage_backend` / `s3_*` / `knowledge_dir`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_storage_unit.py
import pytest
from app.core.storage import LocalFileStorage, S3FileStorage, ObjectNotFoundError

async def test_local_roundtrip(tmp_path):
    s = LocalFileStorage(tmp_path)
    await s.put("kb/a.txt", b"hello", content_type="text/plain")
    assert await s.exists("kb/a.txt")
    assert await s.get("kb/a.txt") == b"hello"
    await s.delete("kb/a.txt")
    assert not await s.exists("kb/a.txt")

async def test_local_missing_raises(tmp_path):
    with pytest.raises(ObjectNotFoundError):
        await LocalFileStorage(tmp_path).get("nope.txt")

async def test_s3_uses_injected_client():
    calls = {}
    class Stub:
        def put_object(self, **kw): calls["put"] = kw
        def get_object(self, **kw): return {"Body": _Body(b"x")}
        def delete_object(self, **kw): calls["del"] = kw
        def head_object(self, **kw): return {}
    s3 = S3FileStorage(Stub(), "bucket", prefix="knowledge/")
    await s3.put("a.md", b"x")
    assert calls["put"]["Bucket"] == "bucket"
    assert calls["put"]["Key"] == "knowledge/a.md"
    assert await s3.get("a.md") == b"x"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_storage_unit.py -v` → FAIL（`ModuleNotFoundError: app.core.storage`）

- [ ] **Step 3: 实现 `app/core/storage.py`**

要点：`LocalFileStorage` 用 `tmp` 后缀 + `replace` 原子写，key 拼接时 `strip("/")` 防目录穿越（`..` 直接拒绝并抛 `ValueError`）；`S3FileStorage` 的 boto3 调用全部包在 `asyncio.to_thread` 里，`get_object` 的 `ClientError` 且 `Error.Code in {"NoSuchKey","404","NotFound"}` 转 `ObjectNotFoundError`；`get_storage()` 按 `settings.storage_backend` 返回并缓存（`s3` 分支用 `boto3.client("s3", endpoint_url=..., aws_access_key_id=..., aws_secret_access_key=..., region_name=...)`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_storage_unit.py -v` → PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/storage.py tests/test_storage_unit.py
git commit -m "feat: local/S3 双后端文件存储抽象"
```

---

### Task 3: Embedding provider（fake / OpenAI）

**Files:**
- Create: `app/modules/knowledge/__init__.py`（空）、`app/modules/knowledge/embedding.py`
- Test: `tests/test_knowledge_embedding_unit.py`

**Interfaces:**
- Produces:
  - `class EmbeddingProvider(Protocol)`: `async embed(self, texts: list[str]) -> list[list[float]]`
  - `class FakeEmbeddingProvider(dim: int)`
  - `class OpenAIEmbeddingProvider(api_key: str, base_url: str, model: str, dim: int, timeout: float, batch_size: int)`
  - `get_embedding_provider(settings: Settings | None = None) -> EmbeddingProvider`（缓存）、`reset_embedding_provider()`
  - `EmbeddingUnavailableError(Exception)`
- Consumes: Task 1 的 `embedding_provider` / `embedding_key_usable`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_knowledge_embedding_unit.py
import math, pytest
from app.modules.knowledge.embedding import FakeEmbeddingProvider, get_embedding_provider

async def test_fake_is_deterministic_and_normalized():
    p = FakeEmbeddingProvider(1536)
    a, b = await p.embed(["退货政策"]), await p.embed(["退货政策"])
    assert a == b
    assert len(a[0]) == 1536
    assert math.isclose(sum(x * x for x in a[0]), 1.0, rel_tol=1e-6)

async def test_fake_ranks_related_above_unrelated():
    p = FakeEmbeddingProvider(1536)
    q, near, far = await p.embed(["退货需要保留吊牌吗"]), \
                   await p.embed(["退货需要保留吊牌吗以及包装"]), \
                   await p.embed(["服务器磁盘告警阈值"])
    dot = lambda x, y: sum(i * j for i, j in zip(x, y))
    assert dot(q[0], near[0]) > dot(q[0], far[0])

async def test_placeholder_key_selects_fake(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "auto")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-xxx")
    assert isinstance(get_embedding_provider(), FakeEmbeddingProvider)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_knowledge_embedding_unit.py -v` → FAIL（模块不存在）

- [ ] **Step 3: 实现**

fake 算法按 spec §8.2：中文 bigram + 英文词 → `blake2b(digest_size=8)` → `index % dim`、符号取哈希首字节奇偶 → TF 累加 → L2 归一化（全零向量返回零向量）。`OpenAIEmbeddingProvider` 用 `httpx.AsyncClient` POST `{base_url}/embeddings`，按 `batch_size` 分批，非 2xx 或结构异常抛 `EmbeddingUnavailableError`。`get_embedding_provider()` 选择顺序：`provider=fake` → fake；`=openai` → 真实（无可用 key 抛 `ValueError`）；`=auto` → `embedding_key_usable` 为真则真实，否则 fake。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_knowledge_embedding_unit.py -v` → PASS

- [ ] **Step 5: 提交**

```bash
git add app/modules/knowledge/__init__.py app/modules/knowledge/embedding.py tests/test_knowledge_embedding_unit.py
git commit -m "feat: fake/OpenAI 双通道 embedding provider"
```

---

### Task 4: 数据模型与迁移

**Files:**
- Create: `app/db/models/knowledge.py`
- Modify: `app/db/models/__init__.py`（导入与 `__all__`）
- Modify: `tests/conftest.py`（`_TABLES` 加两表；强制 `STORAGE_BACKEND=local`、`EMBEDDING_PROVIDER=fake`、`EMBEDDING_API_KEY=""`、`KNOWLEDGE_DIR=data/test-knowledge`）
- Create: `alembic/versions/20261111_1000_<rev>_m4_knowledge.py`
- Test: `tests/integration/test_knowledge_models.py`

**Interfaces:**
- Produces: `KnowledgeDocument`（表 `knowledge_documents`，字段见 spec §5.1）、`KnowledgeChunk`（表 `knowledge_chunks`，字段见 spec §5.2），`KnowledgeDocument.chunks` 关系 `cascade="all, delete-orphan"`
- Consumes: `Settings.embedding_dim`（迁移用）

> 注意：SQLAlchemy 声明式基类已占用 `metadata` 属性，模型里必须写成 `metadata_: Mapped[dict] = mapped_column("metadata", JSONB, ...)`。

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_knowledge_models.py
import uuid
from sqlalchemy import select, text
from app.db.models.knowledge import KnowledgeChunk, KnowledgeDocument

async def test_tables_exist(db):
    async with get_session_factory()() as s:
        cols = (await s.execute(text(
            "select column_name from information_schema.columns where table_name='knowledge_documents'"
        ))).scalars().all()
        assert {"owner_user_id", "filename", "storage_key", "size_bytes", "error"} <= set(cols)

async def test_chunk_cascade_delete(db):
    # 建文档 + 1 个 chunk，删文档后 chunk 数为 0
```

- [ ] **Step 2: 跑测试确认失败** → FAIL（表不存在）

- [ ] **Step 3: 写模型与迁移**

模型按 spec §5.1/§5.2；`embedding` 用 `from pgvector.sqlalchemy import Vector`，`mapped_column(Vector(settings.embedding_dim))`。迁移文件：`op.execute("CREATE EXTENSION IF NOT EXISTS vector")` → 建两表 → 两条部分唯一索引 → `knowledge_chunks` 的 `uq`、HNSW 与 GIN 索引。`downgrade()` 删两表，不回退扩展。

- [ ] **Step 4: 跑测试与迁移检查**

Run: `TEST_DATABASE_URL=… pytest tests/integration/test_knowledge_models.py -v` → PASS
Run: `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` → 无报错

- [ ] **Step 5: 提交**

```bash
git add app/db/models/knowledge.py app/db/models/__init__.py alembic/versions/ tests/integration/test_knowledge_models.py
git commit -m "feat: 知识库文档与分块模型及 pgvector 迁移"
```

---

### Task 5: 文档解析

**Files:**
- Create: `app/modules/knowledge/parsing.py`
- Test: `tests/test_knowledge_parse_unit.py`

**Interfaces:**
- Produces: `@dataclass ParsedDocument: text: str; title: str; metadata: dict[str, Any]`、`parse_document(filename: str, raw: bytes, *, max_pdf_pages: int) -> ParsedDocument`、`UnsupportedDocumentError(ValueError)`
- Consumes: Task 1 的 `knowledge_max_pdf_pages`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_knowledge_parse_unit.py
import pytest
from app.modules.knowledge.parsing import UnsupportedDocumentError, parse_document

def test_markdown_passthrough():
    doc = parse_document("a.md", "# 标题\n\n正文".encode())
    assert "标题" in doc.text and doc.title == "a"

def test_uppercase_extension_ok():
    assert parse_document("A.MD", "正文".encode()).text

def test_csv_rows_flattened():
    doc = parse_document("t.csv", "name,qty\n吊牌,1\n".encode())
    assert "name: 吊牌" in doc.text

def test_unsupported_extension():
    with pytest.raises(UnsupportedDocumentError):
        parse_document("a.exe", b"x")

def test_empty_file_rejected():
    with pytest.raises(ValueError):
        parse_document("a.txt", b"")

def test_pdf_with_text_layer():
    raw = _make_min_pdf("退货需要保留吊牌")
    doc = parse_document("a.pdf", raw, max_pdf_pages=10)
    assert "吊牌" in doc.text and doc.metadata["pages"] == 1

def test_pdf_without_text_layer_fails():
    with pytest.raises(ValueError):
        parse_document("scan.pdf", _make_empty_pdf(), max_pdf_pages=10)
```

`_make_min_pdf` / `_make_empty_pdf` 用 `pypdf` 在测试内生成，不放二进制 fixture。

- [ ] **Step 2: 跑测试确认失败** → FAIL

- [ ] **Step 3: 实现**

扩展名先 `suffix.lower()`；`.txt` 先 `utf-8` 再 `gb18030`；`.pdf` 逐页 `extract_text()`，页数超限抛 `ValueError`，全文空白抛「PDF 未提取到文本（可能是扫描件）」；`.csv` 用 `csv.DictReader` 生成 `"列: 值"` 行；`.json` 递归展平为 `path: value`。`title` 默认取文件名去扩展名。

- [ ] **Step 4: 跑测试确认通过** → PASS

- [ ] **Step 5: 提交**

```bash
git add app/modules/knowledge/parsing.py tests/test_knowledge_parse_unit.py
git commit -m "feat: md/txt/pdf/csv/json 文档解析"
```

---

### Task 6: 文本切分

**Files:**
- Create: `app/modules/knowledge/splitting.py`
- Test: `tests/test_knowledge_split_unit.py`

**Interfaces:**
- Produces: `@dataclass Chunk: content: str; index: int; heading: str; char_start: int`、`split_text(text: str, *, size: int, overlap: int, max_chunks: int) -> list[Chunk]`
- Consumes: 无

- [ ] **Step 1: 写失败测试**

```python
# tests/test_knowledge_split_unit.py
from app.modules.knowledge.splitting import split_text

def test_markdown_headings_recorded():
    text = "# 售后\n\n退货要保留吊牌。\n\n# 产品\n\n新品发布。"
    chunks = split_text(text, size=800, overlap=0, max_chunks=100)
    assert [c.heading for c in chunks] == ["售后", "产品"]
    assert chunks[0].index == 0 and chunks[1].index == 1

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
```

- [ ] **Step 2: 跑测试确认失败** → FAIL

- [ ] **Step 3: 实现**

按 spec §9.2 的分层策略实现；`overlap` 只对「按长度硬切」的相邻块生效；尾部块 < 20 字符并入前块；`char_start` 记录块在原文的起始偏移。

- [ ] **Step 4: 跑测试确认通过** → PASS

- [ ] **Step 5: 提交**

```bash
git add app/modules/knowledge/splitting.py tests/test_knowledge_split_unit.py
git commit -m "feat: Markdown 感知的文本切分"
```

---

### Task 7: 向量检索层

**Files:**
- Modify: `app/db/vector/__init__.py`（当前 0 字节）
- Test: `tests/integration/test_knowledge_vector.py`

**Interfaces:**
- Produces: `@dataclass SearchHit: chunk_id: UUID; document_id: UUID; chunk_index: int; content: str; heading: str; document_title: str; document_filename: str; category: str; updated_at: datetime; score: float`、`async search_chunks(db, *, embedding: list[float], user_id: UUID, top_k: int, threshold: float, categories: list[str] | None) -> list[SearchHit]`
- Consumes: Task 4 的模型；Task 3 的 embedding 向量

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_knowledge_vector.py
async def test_scores_and_orders(db, seeded_docs):
    hits = await search_chunks(db, embedding=await embed("退货吊牌"), user_id=uid,
                               top_k=5, threshold=0.0, categories=None)
    assert hits and hits[0].score >= hits[-1].score
    assert hits[0].document_filename

async def test_category_filter(db, seeded_docs):
    hits = await search_chunks(db, ..., categories=["售后"])
    assert {h.category for h in hits} <= {"售后"}

async def test_excludes_other_users_documents(db, other_user_doc):
    hits = await search_chunks(db, ..., user_id=uid)
    assert other_user_doc.id not in {h.document_id for h in hits}
```

- [ ] **Step 2: 跑测试确认失败** → FAIL

- [ ] **Step 3: 实现**

按 spec §8.3 的 SQL；分类与阈值条件在 Python 侧按是否提供动态拼接；`heading` 从 `chunk.metadata["heading"]` 取、缺省回退 `document_title`；`score` 用 `1 - distance` 并 `round(4)`。

- [ ] **Step 4: 跑测试确认通过** → PASS

- [ ] **Step 5: 提交**

```bash
git add app/db/vector/__init__.py tests/integration/test_knowledge_vector.py
git commit -m "feat: pgvector 相似度检索封装"
```

---

### Task 8: 入库服务与检索编排

**Files:**
- Create: `app/modules/knowledge/repository.py`、`app/modules/knowledge/service.py`
- Modify: `tests/conftest.py`（新增 `user` / `admin` / `fake_storage` / `fake_embedding` / `seeded_docs` fixtures）
- Test: `tests/integration/test_knowledge_service.py`

**Interfaces:**
- Produces（service 层，router 只调用这些）：
  - `async ingest_bytes(db, *, owner: User | None, title: str, filename: str, category: str, raw: bytes) -> KnowledgeDocument`
  - `async list_documents(db, user, *, q, category, status, owner_scope, limit, cursor) -> tuple[list[KnowledgeDocument], str | None, bool, dict[str, int]]`
  - `async get_document(db, user, document_id) -> KnowledgeDocument`
  - `async delete_document(db, user, document_id) -> None`
  - `async retry_document(db, user, document_id) -> KnowledgeDocument`
  - `async search(db, user, *, query: str, top_k: int | None, categories: list[str] | None) -> list[SearchHit]`
  - `async rebuild(db, *, scope: Literal["all","system","own"]) -> dict[str, int]`
  - `visible_filter(user) -> list[ColumnElement]`（自己的 + 系统的）
- Consumes: Task 1 配置、Task 2 `get_storage`、Task 3 `get_embedding_provider`、Task 4 模型、Task 5 `parse_document`、Task 6 `split_text`、Task 7 `search_chunks`

> 阈值规则：`search()` 内部判断 provider 类型，`FakeEmbeddingProvider` 时 `threshold = 0.0`，否则用 `settings.vector_score_threshold`（spec §8.2）。

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_knowledge_service.py
async def test_ingest_creates_ready_document(db, user, fake_storage, fake_embedding):
    doc = await ingest_bytes(db, owner=user, title="退货政策", filename="r.md",
                             category="售后", raw="# 吊牌\n\n退货要保留吊牌。".encode())
    assert doc.status == "ready" and doc.chunk_count >= 1
    assert await fake_storage.exists(doc.storage_key)

async def test_ingest_duplicate_returns_existing(db, user, ...):
    a = await ingest_bytes(db, ..., raw=b"same")
    b = await ingest_bytes(db, ..., raw=b"same")
    assert a.id == b.id

async def test_ingest_parse_failure_marks_failed(db, user, ...):
    doc = await ingest_bytes(db, ..., filename="bad.exe", raw=b"x")
    assert doc.status == "failed" and doc.error

async def test_retry_rereads_original(db, user, ...):
    # 造一个 failed 文档并写入 storage，retry 后变 ready

async def test_retry_missing_object_fails(db, user, ...):
    # 文档的 storage_key 不存在 → status=failed、error 含「原始文件缺失」

async def test_delete_removes_chunks_and_object(db, user, ...)

async def test_visible_filter_hides_other_users(db, user, other_user)

async def test_rebuild_recreates_chunks(db, admin, seeded)
```

- [ ] **Step 2: 跑测试确认失败** → FAIL

- [ ] **Step 3: 实现 repository 与 service**

`ingest_bytes` 顺序：算 checksum → 按归属查重（重复直接返回）→ 建行 `status=indexing` → `await db.flush()` 拿 id → `storage.put(f"{prefix}{id}{ext}", raw)` → 解析/切分/embedding → 批量插 chunk → `status=ready, chunk_count=len(chunks)` → commit；任一步异常 → `status=failed` + `error`（截断 500）→ commit 后返回文档（不抛给调用方，让前端看到失败原因）。`rebuild` 需 `admin` 校验由 router 做，service 只管执行。

- [ ] **Step 4: 跑测试确认通过** → PASS

- [ ] **Step 5: 提交**

```bash
git add app/modules/knowledge/repository.py app/modules/knowledge/service.py tests/integration/test_knowledge_service.py
git commit -m "feat: 知识库入库、检索、重试与重建服务"
```

---

### Task 9: API 层

**Files:**
- Create: `app/modules/knowledge/schemas.py`、`app/modules/knowledge/router.py`
- Modify: `app/api/v1/router.py`（注册 + `COMMON_ERROR_RESPONSES` 补 503）
- Test: `tests/integration/test_knowledge_api.py`

**Interfaces:**
- Produces: 7 个端点的 Pydantic 模型 —— `KnowledgeDocumentOut`（11 字段，对齐前端）、`KnowledgeDocumentPage`（`items/next_cursor/has_more/stats`）、`KnowledgeSearchHitOut`（10 字段）、`KnowledgeSearchOut`（`hits/elapsed_ms`）、`KnowledgeSearchRequest`（`query/top_k/filters.category`）、`KnowledgeRebuildRequest/Out`
- Consumes: Task 8 的 service 函数

- [ ] **Step 1: 写失败测试**（覆盖 Review Focus 第 1、3 条）

```python
# tests/integration/test_knowledge_api.py
async def test_upload_list_detail_delete(client, auth):
    r = await client.post("/api/v1/knowledge/documents", files={"file": ("a.md", b"# t\n\nx", "text/markdown")},
                          data={"category": "售后"}, headers=auth)
    assert r.status_code == 201 and r.json()["status"] == "ready"

async def test_list_stats_shape(client, auth):
    r = await client.get("/api/v1/knowledge/documents", headers=auth)
    assert set(r.json()["stats"]) == {"documents","chunks","ready","indexing","pending","failed"}

async def test_oversize_upload_413(client, auth, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_MAX_UPLOAD_BYTES", "10"); reset_settings_cache()
    r = await client.post(..., files={"file": ("a.md", b"x" * 100, "text/markdown")}, headers=auth)
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"

async def test_unsupported_extension_422(client, auth)
async def test_duplicate_returns_200(client, auth)
async def test_other_user_404(client, second_client, auth)
async def test_rebuild_forbidden_for_normal_user(client, auth)   # 403 forbidden
async def test_owner_scope_all_forbidden(client, auth)           # 403
async def test_knowledge_disabled_403(client, auth, monkeypatch)

async def test_concurrent_duplicate_upload_single_row(client, auth):
    same = b"# t\n\nsame content"
    responses = await asyncio.gather(*[
        client.post("/api/v1/knowledge/documents",
                    files={"file": ("a.md", same, "text/markdown")}, headers=auth)
        for _ in range(3)
    ])
    assert len({r.json()["id"] for r in responses}) == 1
    assert all(r.status_code in (200, 201) for r in responses)

async def test_concurrent_rebuild_keeps_chunk_count_consistent(client, admin_auth, seeded)
```

- [ ] **Step 2: 跑测试确认失败** → FAIL（404，路由不存在）

- [ ] **Step 3: 实现 schemas 与 router**

上传用 `UploadFile`，先判 `len(raw)` 超限抛 `PayloadTooLargeError`；`scope=system` 需 `user.role == "admin"` 否则 `ForbiddenError`；`owner_scope=all` 同样。`rebuild` 用 `require_admin` 依赖。列表 `stats` 按可见范围全量统计（6 个键）。

- [ ] **Step 4: 跑测试确认通过** → PASS

- [ ] **Step 5: 提交**

```bash
git add app/modules/knowledge/schemas.py app/modules/knowledge/router.py app/api/v1/router.py tests/integration/test_knowledge_api.py
git commit -m "feat: 知识库 7 个 API 端点"
```

---

### Task 10: 聊天集成（`search_knowledge` 工具 + citations）

**Files:**
- Modify: `app/modules/chat/tools.py`
- Modify: `app/modules/chat/registry.py`（`RunContext.citations`）
- Modify: `app/modules/chat/service.py`（工具装配 + 4 处终态）
- Test: `tests/integration/test_knowledge_chat.py`

**Interfaces:**
- Produces: `build_search_knowledge_tool(runner) -> StructuredTool`、`ToolRunner.__init__` 新增 `citations: list[dict]` 与 `knowledge_categories: list[str]`、`RunContext.citations: list[dict]`
- Consumes: Task 8 的 `search`、Task 9 的 `CitationOut` 形状

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_knowledge_chat.py
async def test_tool_call_emits_citations(client, auth, seeded_kb, monkeypatch):
    # 让假模型先调 search_knowledge 再回答
    events = await collect_sse(client, headers=auth, payload={"message": "退货要吊牌吗？"})
    tool_result = next(d for e, d in events if e == "tool.result")
    assert tool_result["citations"]
    done = next(d for e, d in events if e == "message.done")
    assert done["citations"] and done["citations"][0]["index"] == 1

async def test_citations_persisted(client, auth, seeded_kb):
    # GET /conversations/{id}/messages 的助手消息 citations 非空

async def test_citation_indexes_contiguous_and_unique(client, auth, seeded_kb):
    # 两次工具调用后 index 从 1 连续、chunk_id 不重复
```

- [ ] **Step 2: 跑测试确认失败** → FAIL

- [ ] **Step 3: 实现**

`search_knowledge` 工具内部 `async with get_session_factory()() as db:` 取 `User` 后调 service；结果文本按 `[n] heading（filename / category / score）` 格式；命中后追加 `runner.citations`。`execute_run` 构造 `ToolRunner` 时传 `citations=context.citations`、`knowledge_categories=run_capabilities.get("knowledge_categories", [])`；`tools` 列表追加该工具。终态按 spec §10.3 精确点：`execute_run` 早取消（~L328）、成功（~L574）、`_finalize_canceled`（~L726）、`_finalize_interrupted`（~L795）填 `context.citations`；`_finalize_success/canceled/interrupted/failed` 写 `assistant.citations`。

- [ ] **Step 4: 跑测试确认通过** → PASS

- [ ] **Step 5: 提交**

```bash
git add app/modules/chat/tools.py app/modules/chat/registry.py app/modules/chat/service.py tests/integration/test_knowledge_chat.py
git commit -m "feat: search_knowledge 工具与引用落库"
```

---

### Task 11: 能力开关与系统提示

**Files:**
- Modify: `app/modules/system/service.py`、`app/modules/chat/capabilities.py`、`app/modules/chat/schemas.py`、`app/agents/prompts.py`
- Test: `tests/integration/test_knowledge_capability.py`

**Interfaces:**
- Produces: `system/info.capabilities.knowledge = settings.knowledge_enabled`；`KNOWLEDGE_SECTION` 常量；`validate_chat_selections` 对 `knowledge_categories` + `knowledge_enabled=False` 抛 `CapabilityDisabledError`
- Consumes: Task 1 的 `knowledge_enabled`

- [ ] **Step 1: 写失败测试**

```python
async def test_system_info_reports_knowledge_true(client, auth)
async def test_knowledge_false_blocks_categories(client, auth, monkeypatch)  # 403 capability_disabled
async def test_prompt_includes_knowledge_rules_when_docs_ready(...)         # 直接测 assemble_system_prompt
async def test_prompt_omits_rules_when_no_docs(...)
```

- [ ] **Step 2: 跑测试确认失败** → FAIL

- [ ] **Step 3: 实现**（提示词按 spec §10.2 原文写入）

- [ ] **Step 4: 跑测试确认通过** → PASS

- [ ] **Step 5: 提交**

```bash
git add app/modules/system/service.py app/modules/chat/capabilities.py app/modules/chat/schemas.py app/agents/prompts.py tests/integration/test_knowledge_capability.py
git commit -m "feat: knowledge 能力开关与知识库提示词"
```

---

### Task 12: `scripts/build_kb.py`

**Files:**
- Create: `scripts/build_kb.py`
- Test: `tests/integration/test_build_kb.py`

**Interfaces:**
- Produces: `async def build(source: Path, *, category: str, owner_email: str | None, exts: set[str], rebuild: bool, dry_run: bool) -> dict[str, int]`，命令行 `python scripts/build_kb.py --source DIR ...`
- Consumes: Task 8 的 `ingest_bytes`

- [ ] **Step 1: 写失败测试**

```python
async def test_build_imports_and_counts(db, tmp_path):
    (tmp_path / "a.md").write_text("# t\n\n内容")
    result = await build(tmp_path, category="售后", owner_email=None, exts={".md"}, rebuild=False, dry_run=False)
    assert result["created"] == 1

async def test_build_skips_duplicates(db, tmp_path)
async def test_build_dry_run_writes_nothing(db, tmp_path)
```

- [ ] **Step 2: 跑测试确认失败** → FAIL

- [ ] **Step 3: 实现**（递归遍历、按扩展名过滤、复用 `ingest_bytes`、打印 `新增/跳过/失败` 汇总）

- [ ] **Step 4: 跑测试确认通过** → PASS

- [ ] **Step 5: 提交**

```bash
git add scripts/build_kb.py tests/integration/test_build_kb.py
git commit -m "feat: 批量导入知识库脚本"
```

---

### Task 13: 离线评测集

**Files:**
- Create: `tests/fixtures/knowledge_docs/*.md`（8–10 篇中文文档）
- Create: `tests/fixtures/knowledge_eval.jsonl`
- Test: `tests/integration/test_knowledge_eval.py`

**Interfaces:**
- Produces: 评测用例（`id/question/doc/keyword`）；`pytest.mark.skipif` 区分链路组与语义组
- Consumes: Task 8 的 `ingest_bytes` / `search`

- [ ] **Step 1: 造文档与用例**（每篇文档 300–800 字，覆盖售后/产品/安全/计费/运维；用例 20 条：链路组 15、语义组 5）

- [ ] **Step 2: 写测试**

```python
async def test_chain_group_top1_hits(db, seeded_fixture_docs):
    for case in chain_cases:
        hits = await search(db, user, query=case["question"], top_k=3, categories=None)
        assert hits and hits[0].document_filename == case["doc"]
        assert case["keyword"] in hits[0].content

@pytest.mark.skipif(not real_embedding_available(), reason="需要可用的 EMBEDDING_API_KEY")
async def test_semantic_group_recall(db, seeded_fixture_docs):
    # top-3 命中率 >= 90%
```

- [ ] **Step 3: 跑测试**

Run: `pytest tests/integration/test_knowledge_eval.py -v`
Expected: 链路组 PASS；语义组 SKIPPED（无真 key）

- [ ] **Step 4: 提交**

```bash
git add tests/fixtures/knowledge_docs tests/fixtures/knowledge_eval.jsonl tests/integration/test_knowledge_eval.py
git commit -m "test: 知识库离线评测集与召回率用例"
```

---

### Task 14: 文档更新与最终验证

**Files:**
- Modify: `docs/database.md`（§4.8/§4.9）、`docs/api.md`（§1、§2.6、§4.5、§7.4、§7.9、§8.2）、`docs/architecture.md`（§6 模块表、§13.2）、`docs/roadmap.md`（M4 勾选与证据）、`docs/development.md`、`tests/README.md`
- Create: `docs/feedback/2026-11-11-m4-backend-report.md`
- Test: 无新测试；跑全量 + OpenAPI diff

**Interfaces:** 无

- [ ] **Step 1: 更新文档**（按 spec §13 的逐条清单；roadmap 勾选必须能对应到已跑的测试或实测输出）

- [ ] **Step 2: 全量测试**

Run: `TEST_DATABASE_URL=postgresql+psycopg://devuser:devpass@localhost:54321/agent_hub_test pytest -q`
Expected: 108 基线 + 新增全通过，0 failed

- [ ] **Step 3: OpenAPI 与检索基准**

Run: `python scripts/export_openapi.py`（确认含 7 个 knowledge 端点）
Run: 检索基准脚本跑 50 次，记录 P50/P95 与实际文档/chunk 规模，写入反馈文档

- [ ] **Step 4: 真实联调证据**

启动后端（`uv run uvicorn app.main:app --port 8000`）后依次执行并留存输出：

```bash
# 1. 用 seed 账号登录拿 token
# 2. 上传 3 篇测试文档 → 确认 status=ready
# 3. POST /api/v1/knowledge/search → 确认 hits 非空、score 合理
# 4. POST /api/v1/chat/stream → 抓 SSE，确认 tool.start / tool.result(带 citations)
#    / message.done(带 citations) 三条事件齐全
# 5. GET /api/v1/conversations/{id}/messages → 确认助手消息 citations 非空（刷新仍可见）
```

证据（SSE 片段、search JSON、messages JSON、P95 数值）写入 `docs/feedback/2026-11-11-m4-backend-report.md`。

- [ ] **Step 4: 提交**

```bash
git add docs/
git commit -m "docs: M4 知识库 RAG 文档与验收证据"
```
