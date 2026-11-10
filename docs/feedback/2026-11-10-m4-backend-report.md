# M4 知识库 RAG 后端交付报告

- 日期：2026-11-10
- 范围：M4 知识库 RAG 后端全量（**不含前端**）
- 设计文档：[M4 知识库 RAG 设计](../superpowers/specs/2026-11-10-m4-knowledge-rag-design.md)
- 实施计划：[M4 知识库 RAG 实施计划](../superpowers/plans/2026-11-10-m4-knowledge-rag.md)
- 前端契约：`agent-hub-web` 的 `src/types/knowledge.ts`、`conversation.ts#Citation`、`api.ts#PageResult`（已冻结，本次逐字段对齐）

## 1. 交付内容

| 层 | 文件 | 说明 |
|---|---|---|
| 配置 | `app/core/config.py` | 17 个知识库/存储/Embedding 新配置项 |
| 存储 | `app/core/storage.py` | `FileStorage` 协议 + `LocalFileStorage` + `S3FileStorage`（注入 client） |
| 时间 | `app/core/timeutil.py` | `elapsed_ms()`，耗时夹紧到 0（时钟回退保护） |
| 解析 | `app/modules/knowledge/parsing.py` | md/markdown/txt/pdf/csv/json |
| 切分 | `app/modules/knowledge/splitting.py` | Markdown 标题分层 + 段落聚合 + 句切/硬切 + overlap |
| Embedding | `app/modules/knowledge/embedding.py` | fake（signed hashing）/ OpenAI 兼容双通道 |
| 向量 | `app/db/vector/__init__.py` | `SearchHit` + `search_chunks`（pgvector `<=>`） |
| 数据 | `app/db/models/knowledge.py` + 迁移 `c4e7a91b2f36` | 两表、两条部分唯一索引、HNSW、GIN |
| 服务 | `app/modules/knowledge/{repository,service}.py` | 入库/列表/详情/删除/重试/检索/重建 |
| API | `app/modules/knowledge/{schemas,router}.py` | 7 个端点 |
| 聊天 | `app/modules/chat/{tools,service,registry}.py` | `search_knowledge` 工具 + 引用收集/落库/SSE |
| 开关 | `app/modules/system/service.py`、`chat/service.py`、`chat/schemas.py`、`agents/prompts.py` | `capabilities.knowledge`、请求门禁、系统提示 |
| 脚本 | `scripts/build_kb.py` | 批量导入 + dry-run + rebuild |
| 测试 | `tests/` 下 8 个新文件 + `conftest.py` 夹具 | 见 §2 |

依赖新增：`pypdf`、`boto3`。

## 2. 实测证据

### 2.1 测试

```
TEST_DATABASE_URL=postgresql+psycopg://devuser:devpass@localhost:54321/agent_hub_test \
  .venv/bin/python -m pytest -q
→ 339 passed, 1 skipped in ~46s
```

- 基线 M1.5/M2 时期 108 项 → 现在 **339 项通过**；
- 唯一的 skip 是语义评测组（需真实 Embedding key，见 §4.1）；
- **连续 10 次全量运行 0 失败**（修复时钟回退问题后，见 §5）。

### 2.2 离线评测（spec §11.2）

`tests/integration/test_knowledge_eval.py`：

- 链路组 **15/15** 条用例 top-1 命中预期文档且命中片段包含预期关键词；
- 语义组 5 条在 fake embedding 下**全部未命中**（诚实性守卫，证明这组测的确实是语义能力而非词面匹配）；
- 语义组在标准测试中 skip，手动运行方式见 `tests/integration/test_knowledge_eval.py` 模块 docstring（`KNOWLEDGE_EVAL_REAL=1`）。

> 结论口径：**语义召回率 ≥90% 这一项没有取得证据**，因为本机没有可用的 Embedding 密钥。不得据此打勾。

### 2.3 真实 HTTP 联调（`uvicorn` + 真实 PG）

开发库执行迁移 `8a2f7c31e9d5 → c4e7a91b2f36` 后：

| 操作 | 结果 |
|---|---|
| `GET /system/info` | `capabilities.knowledge = true`，`embedding = {model: text-embedding-3-small, dim: 1536}` |
| `POST /knowledge/documents` ×3 | 全部 **201**、`status=ready`、`chunk_count=2`、`size_bytes` 正确 |
| `GET /knowledge/documents` | `stats = {documents:3, chunks:6, ready:3, indexing:0, pending:0, failed:0}`；文档对象恰好 11 字段 |
| `POST /knowledge/search` | 命中 3 条，top-1 为「退货政策 > 吊牌要求」，`score=0.2552`；hit 恰好 10 字段 |
| 分类过滤 | `filters.category=内部资料` 生效 |

检索基准（50 次，5 条 query 循环 10 轮，规模 **3 文档 / 6 分块**）：

| 指标 | 服务端 `elapsed_ms` | 端到端 HTTP |
|---|---|---|
| P50 | 2 ms | 4.8 ms |
| P95 | **2 ms** | 7.8 ms |
| max | 3 ms | 9.4 ms |

> 规模说明：3 文档 6 分块远小于 roadmap 说的「百万级 chunk 以内」。P95 < 200ms 达标，但**不能据此推断百万级表现**；容量基线列入 M6。

### 2.4 引用链路（spec §10.3）

fake 模型不会发起工具调用，因此只替换 `chat_service.build_chat_model` 一个接缝（与集成测试同一做法），HTTP/SSE/LangGraph/ToolRunner/终态/落库全部真实执行：

```
--- tool.start ---   tool=search_knowledge
--- tool.result ---  status=ok, summary="检索到 1 条片段",
                     citations=[{index:1, document_id, chunk_id, title:"退货政策",
                                 category:"售后", score:0.3283, updated_at}]
--- message.done --- finish_reason=stop, duration_ms=137, citations=[同上]
--- GET /conversations/{id}/messages ---
assistant.citations = [同上]      ← 刷新后仍在（落库生效）
结论：SSE 与落库一致、字段与 CitationOut 对齐 = True
```

## 3. 与设计文档的偏离（均已在实现时拍板）

| # | 偏离 | 原因 |
|---|---|---|
| 1 | `stats` 是**可见范围全量**，不随 `q`/`category`/`status` 变化 | 前端 mock `listKnowledgeDocuments` 就是这么算的；`PageResult.stats` 的「筛选口径」只约束 api.md §7.6 列举的四个端点 |
| 2 | 上传幂等返回 **200**（不是附件的 201 + `duplicate`） | 不往被冻结的 `KnowledgeDocument` 里加字段 |
| 3 | `rebuild(scope="own")` = **所有用户的私有文档** | rebuild 仅管理员，三种 scope 是「范围」切法而非「归属」 |
| 4 | `rebuild` 返回的 `documents` 含失败数、`failed` 是其中失败条数 | 总数 + 失败数最可解释 |
| 5 | 检索 `elapsed_ms` 返回真实耗时（spec 示例写 `0`） | 真实耗时更有用；示例值非契约 |
| 6 | `knowledge_categories` 的 403 门禁落在 `chat/service.py::_check_capabilities`（spec 写的是 `validate_chat_selections`） | 行为等价，且只有一处检查 |
| 7 | `knowledge_documents.filename` 在脚本导入时存相对路径 | 需同时满足「`source_uri` 为相对路径」与「rebuild 按 source_uri 定位」；检索会因为 `document_filename` 显示相对路径 |
| 8 | `knowledge_chunks` 不加 `updated_at`，检索时间统一取文档的 | chunk 创建后不变，文档时间才是用户感知的「资料更新时间」 |
| 9 | JWT 解码加 5 秒 leeway | 本机时钟回退导致令牌 `iat` 落在未来（详见 §5） |

## 4. 未做 / 未取得证据

### 4.1 未取得证据

- **语义召回率 ≥ 90%**：本机 `EMBEDDING_API_KEY` 为占位符，无法跑真实语义检索。逃生舱与手动命令已就绪，填入真实 key 后执行即可。

### 4.2 明确的未做项（非 M4 范围或后续里程碑）

- 前端 M4（知识库页上传/检索调试/引用展示/工具卡/分类菜单）；
- 混合检索（BM25 + RRF）、reranker（architecture.md §12 已列为演进方向）；
- OCR（扫描版 PDF 报可读错误，不做图像识别）；
- 对象存储的真实 minio 联调（代码已支持 `STORAGE_BACKEND=s3` 与注入 client 的单元测试，未对本机 minio 做端到端验证）；
- 长期记忆的语义检索（评估后不做，理由见设计文档 §8.4）；
- 归档任务清理孤儿对象（M6）。

## 5. 实施中发现并修复的既有问题

### 5.1 系统时钟回退导致的偶发测试失败

排查过程与证据见 `.superpowers/sdd/2026-11-10-m4-knowledge-rag/progress.md`。摘要：

- 现象：全量测试偶发失败，每次失败的用例都不同（401、负耗时断言、`KeyError: items` 等）；
- 诊断：在 `decode_token` 注入日志后抓到 `ImmatureSignatureError`；同时 200 次时钟采样测到 **70 次回退，单次最多 -494ms**；
- 修复：JWT 解码加 5 秒 leeway；新增 `app/core/timeutil.py::elapsed_ms()` 并替换 11 处裸时间差计算；
- 验证：修复前 8 次全量 2 次失败 → 修复后 **10 次全量 0 次失败**。

### 5.2 其余顺带修复

- `_commit_and_refresh`：提交后 ORM 属性过期，async 会话下直接序列化会抛 `MissingGreenlet`；
- S3 的 `404` 状态码兜底会吞掉 `NoSuchBucket` 等存储故障，已收窄为仅按 `Error.Code` 判定；
- `tool.result` 在工具异常分支存在未绑定变量风险（`UnboundLocalError` 会打断整轮），已初始化并加回归用例。

### 5.3 最终整分支审查的修复轮（commit `82bfade`）

全分支审查给出 0 Critical / 6 Important，全部在同一轮修复：

| # | 问题 | 修复 |
|---|---|---|
| 1 | `auto` 模式在**生产**也会静默回退 fake embedding，违反 spec §8.1 | `validate_runtime()` 在 prod 缺可用 key 时拒绝启动（`EMBEDDING_PROVIDER=fake` 是运维显式选择，仍然允许）；`auto` 分支 prod 不再回退 fake |
| 2 | 上传先 `read()` 整个请求体再判长度，超大文件可打满内存 | 改为 `read(max_bytes + 1)`，与附件模块一致，超限立即 413 |
| 3 | 超长 `filename` / 派生 `title` 触发 `DataError` → **500** | `_validate_upload` 增加 255/200 字符校验，返回 422 |
| 4 | 非法 `EMBEDDING_BASE_URL`（`httpx.InvalidURL` 非 `HTTPError` 子类）与缺 key 的 `ValueError` 在检索上变成 **500** | 一并包装/映射为 503 `service_unavailable` |
| 5 | 时钟修复漏了两处 reasoning 耗时 | 改用 `elapsed_ms()`；全仓 grep 只剩 `timeutil.py` 与 `a2a/taskstate.py` |
| 6 | `docs/openapi.json` 仍是 M2 版本，缺全部 M4 端点 | 重新导出并提交；`grep -c '"/api/v1/knowledge'` = 5 |

并入的两个低成本项：`retry_document` 加行锁（消除与 rebuild 的写冲突窗口）；`_failure_message` 只透传中文文案，英文 `ValueError` 走通用中文兜底。

修复后：**339 passed, 1 skipped**（新增 16 例）、连续 2 次全量稳定；`ruff check app/ tests/ scripts/` 通过。

复审残留 2 个 Low（已记录，未在本轮处理）：
- 派生 `title` 校验对「显式短 title + 201–255 字符 filename」过度拦截（该组合修复前可成功）；
- `except (EmbeddingUnavailableError, ValueError)` 恰好包住 `provider.embed`，未来若 `embed` 内部出现编程性 `ValueError` 会被误报成 503。

## 6. 给前端的契约说明

### 6.1 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/knowledge/documents` | multipart（`file` + 可选 `title`/`category`/`scope`）；新建 **201**、同内容重复 **200** |
| GET | `/api/v1/knowledge/documents` | `q`/`category`/`status`/`owner_scope`/`limit`/`cursor` |
| GET | `/api/v1/knowledge/documents/{id}` | 详情 |
| DELETE | `/api/v1/knowledge/documents/{id}` | 204 |
| POST | `/api/v1/knowledge/documents/{id}/retry` | 失败重试 |
| POST | `/api/v1/knowledge/search` | `{query, top_k?, filters?: {category?}}` |
| POST | `/api/v1/knowledge/rebuild` | 仅管理员，`{scope: all\|system\|own}` |

### 6.2 形状（已与前端类型逐字段核对）

- 文档对象 **11 字段**：`id, title, source_uri, category, status, chunk_count, size_bytes, owner, error, created_at, updated_at`（**没有** `filename`）
- 检索 hit **10 字段**：`chunk_id, document_id, title, document_title, document_filename, category, chunk_index, content, score, updated_at`
- `Citation` **7 字段**：`index, document_id, chunk_id, title, category, score, updated_at`（不带 content）
- `stats` **6 键**：`documents, chunks, ready, indexing, pending, failed`

### 6.3 前端需要知道的行为

1. `capabilities.knowledge` 现在默认 **true**，路由守卫与侧边栏会放行；
2. 请求携带 `knowledge_categories` 不再 403，它会作为 `search_knowledge` 的**默认分类过滤**；
3. `KNOWLEDGE_ENABLED=false` 时：所有知识库端点 403 `capability_disabled`，且聊天里的工具会返回「知识库能力未启用」、不产生引用；
4. 上传超限是 **413**（不是 422）；
5. embedding 服务故障时检索返回 **503 `service_unavailable`**；
6. `owner` 为 `null` 表示系统文档。

## 7. 环境前置（部署/联调必读）

1. **必须执行迁移**：`alembic upgrade head`。开发库没跑迁移时，上传会 500（`relation "knowledge_documents" does not exist`）——本次联调就踩到了这一点，测试用 `create_all` 建表所以掩盖了它。
2. pgvector 扩展：`CREATE EXTENSION IF NOT EXISTS vector`（迁移里会执行，需要 superuser 或已预装 `postgresql-18-pgvector`）。
3. 存储后端：`STORAGE_BACKEND=local`（默认，`KNOWLEDGE_DIR`）或 `s3`（`S3_*` 六项）。
4. Embedding：`EMBEDDING_PROVIDER=auto` 时，**dev/test** 下 key 为空或命中占位符规则会回退 fake；**prod 下不会静默降级**——缺少可用 key 会在启动时直接报错（`RuntimeError`）。若生产确实要跑 fake（例如纯演示环境），必须显式设置 `EMBEDDING_PROVIDER=fake`。

## 8. 已知限制

| 限制 | 影响 | 处置 |
|---|---|---|
| fake embedding 的分数不可解释（短 query 对长 chunk 余弦上界约 0.11） | 开发/测试环境的 `score` 只可用于排序，不能当相关性阈值 | fake 模式阈值自动置 0；真实 provider 才用 `VECTOR_SCORE_THRESHOLD` |
| 上传是同步入库 | 大文件 + 真实 embedding 可能接近请求超时 | 10MB 上限 + 分页数上限；未来可异步化，状态机已保留四态 |
| PDF 只提取文本层 | 扫描件报「可能是扫描件」，中文 CID 字体提取质量取决于 pypdf | 不做 OCR（M4 非目标） |
| 检索的 owner 过滤是 join 后过滤 | 数据量增大后 HNSW 可能返回少于 top_k | 已记录，容量优化列入 M6 |
| 重建会让历史 `chunk_id` 失效 | 旧引用的精确跳转失效 | `Citation` 是快照，前端按 title 跳页，展示不受影响 |
| 派生 title 长度校验对「短显式 title + 长 filename」过度拦截 | 该组合返回 422（修复前可入库） | 复审残留 Low，后续改为「仅未显式提供 title 时校验派生值」 |
| `search` 的 `except (EmbeddingUnavailableError, ValueError)` 覆盖面略宽 | 未来若 `embed` 内部抛编程性 `ValueError` 会被误报成 503 | 复审残留 Low，当前无实际触发路径 |
