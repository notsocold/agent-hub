# Agent Hub 项目初始化设计

- 日期：2026-11-04
- 状态：已确认（M0 结构初始化）
- 需求来源：用户直接委托
- 参考项目：`/home/zhaodan/work/PythonProjects/customer-service`
- 配套前端：`/home/zhaodan/work/NodeJsProjects/agent-hub-web`

## 1. 背景与目标

需要一个面向用户的 **多智能体对话平台** 的项目骨架：

1. 用户与 AI 进行多轮对话，回答流式输出；
2. AI 通过 **Google A2A 协议** 调用其他智能体完成任务；
3. 使用 LangChain + LangGraph 做智能体编排，使用向量数据库做知识检索；
4. 项目结构参考 `customer-service`，但数据库层必须目录化，不能再是单个 `db.py`；
5. 本阶段只初始化 **项目结构、声明式配置与文档**，**不写任何实现代码、不安装依赖、不启动容器**；后续在新工作区继续。

配套一个 Vue 前端项目，作为该服务的 UI。

## 2. 命名

| 项目 | 名称 | 目录 |
|---|---|---|
| 后端 | `agent-hub` | `/home/zhaodan/work/PythonProjects/agent-hub` |
| 前端 | `agent-hub-web` | `/home/zhaodan/work/NodeJsProjects/agent-hub-web` |

命名理由：Hub 表达「用户对话入口 + 多智能体汇聚/调度中枢」，与现有 `a2a-backend`、`a2a-ui`、`a2a-agents` 区分；`-web` 后缀明确前端定位。

## 3. 技术选型

| 组件 | 选型 | 版本基线 | 理由 |
|---|---|---|---|
| Web | FastAPI + Uvicorn | ≥ 0.141 / ≥ 0.53 | 与 customer-service 一致，异步生态成熟 |
| 编排 | LangChain + LangGraph | 1.4.x / 1.2.x | 用户指定；LangGraph 状态图适合多轮与工具循环 |
| A2A | `a2a-sdk[fastapi]` | 1.3.x | 实现 A2A 1.0，兼容 0.3；官方 Python SDK |
| 业务库 | PostgreSQL 16 + SQLAlchemy 2（异步） | — | 事务、JSONB、扩展生态 |
| 向量库 | pgvector（HNSW + cosine） | 0.5.x | 与业务同库同事务；用户已有 Docker |
| 会话记忆 | `langgraph-checkpoint-postgres` | 3.2.x | 多实例可共享，避免 SQLite 单机限制 |
| 迁移 | Alembic | ≥ 1.20 | SQLAlchemy 官方迁移工具 |
| 配置 | pydantic-settings | ≥ 2.15 | 类型安全、与 customer-service 一致 |
| 认证 | PyJWT + pwdlib(argon2) | — | FastAPI 官方推荐 pwdlib；避免 passlib 兼容问题 |
| 前端 | Vue 3 + TS + Vite + Pinia + Vue Router + Element Plus | — | 与现有 Node 项目技术栈一致 |
| Python 包管理 | uv | ≥ 0.11 | 与 customer-service 一致 |
| 前端包管理 | pnpm | ≥ 10 | 用户偏好 |

### 向量库为什么选 pgvector 而不是 Qdrant

- 业务元数据（文档、权限、分类）在 PostgreSQL，向量与元数据同库可以 join/过滤/事务级联删除；
- 团队级规模（10 万–1000 万 chunk）HNSW 足够，查询 P95 可控；
- 运维面最小：一套备份、监控、连接池、迁移；
- 已通过 `VectorStore` Protocol 预留切换能力，出现瓶颈时可平滑迁移 Qdrant。

## 4. 目录结构

```text
agent-hub/
├── app/
│   ├── main.py
│   ├── api/v1/router.py
│   ├── core/{config,logging,middleware,exceptions}.py
│   ├── db/
│   │   ├── base.py / engine.py / session.py / mixins.py
│   │   ├── models/            # 按领域拆分
│   │   └── vector/{store.py,pgvector_store.py}
│   ├── agents/                # LangGraph 图、状态、提示词、工具
│   └── modules/               # auth / conversations / chat / agents / a2a / knowledge / tasks / approvals
├── alembic/
├── docker/initdb/
├── docker-compose.yml
├── scripts/
├── tests/
├── data/
├── docs/
├── pyproject.toml
├── alembic.ini
├── Makefile
├── .env.example
└── README.md
```

与 customer-service 的核心差异：

- `app/db.py` → `app/db/` 目录（base/engine/session/mixins/models/vector）；
- ORM 模型从各业务模块内 `models.py` 收敛到 `app/db/models/<领域>.py`；
- 新增 `app/agents/`（LangGraph 领域层）与 `app/modules/a2a/`；
- 新增 `docker-compose.yml`、`docker/initdb/`；
- 存储从 SQLite + FAISS 改为 PostgreSQL + pgvector。

## 5. 模块职责

| 模块 | 职责 |
|---|---|
| `core` | 配置、日志、中间件、异常、鉴权原语 |
| `db` | 引擎、会话、模型、向量存储抽象 |
| `agents` | LangGraph 图、状态、提示词、知识检索工具、远程 Agent 工具 |
| `modules/auth` | 注册/登录/刷新/当前用户 |
| `modules/conversations` | 会话与消息历史 |
| `modules/chat` | SSE 流式编排、run 生命周期、取消 |
| `modules/agents` | 远程 Agent 注册、Card 缓存、健康检查 |
| `modules/a2a` | A2A Server/Client、Agent Card、任务映射 |
| `modules/knowledge` | 文档入库、切分、Embedding、检索 |
| `modules/tasks` | run / A2A Task 查询与取消 |
| `modules/approvals` | HITL 审批与恢复 |

分层规则见 `app/modules/README.md` 与 `docs/architecture.md` 第 6 节。

## 6. 数据库设计概要

一个 PostgreSQL 数据库，`public` 放业务表，`langgraph` schema 放 checkpoint。

表清单：

| 表 | 用途 |
|---|---|
| `users` | 用户与登录凭据 |
| `conversations` | 会话（= LangGraph thread） |
| `messages` | 用户/助手/工具消息 |
| `runs` | 一次图执行的运行记录 |
| `remote_agents` | 远程 A2A Agent 注册表与 Card 快照 |
| `a2a_tasks` | 远程任务生命周期 |
| `tool_invocations` | 工具调用审计 |
| `knowledge_documents` | 知识文档 |
| `knowledge_chunks` | 文本块 + `vector(1536)` |
| `approvals` | 人工审批工单 |
| `audit_logs` | 安全/管理审计 |

详细字段、索引、状态机见 `docs/database.md`。

## 7. 接口设计

用户侧 REST + SSE，A2A 侧 JSON-RPC：

| 类别 | 端点 |
|---|---|
| 认证 | `/api/v1/auth/{register,login,refresh,logout,me}` |
| 会话 | `/api/v1/conversations[...]` |
| 聊天 | `POST /api/v1/chat/stream`（SSE） |
| 运行 | `/api/v1/runs/{id}`、`/cancel`、`/events` |
| 远程 Agent | `/api/v1/agents[...]`、`/refresh`、`/health` |
| 任务 | `/api/v1/tasks[...]`、`/cancel` |
| 知识库 | `/api/v1/knowledge/[...]`、`/search`、`/rebuild` |
| 审批 | `/api/v1/approvals[...]`、`/decision` |
| 系统 | `/healthz`、`/readyz`、`/api/v1/system/info` |
| A2A | `POST /a2a`（JSON-RPC）、Agent Card well-known 路径 |

SSE 事件、错误码、示例见 `docs/api.md`。

## 8. 配置项

完整清单见 `.env.example`，分组：

- 应用：`APP_NAME`、`ENV`、`DEBUG`、`API_PREFIX`、`CORS_ORIGINS`
- 数据库：`DATABASE_URL`、连接池、`CHECKPOINT_DATABASE_URL`、`CHECKPOINT_SCHEMA`
- 模型：`LLM_PROVIDER`、`LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`、超时/温度
- Embedding：`EMBEDDING_API_KEY`、`EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`、`EMBEDDING_DIM`
- 检索：`VECTOR_TOP_K`、`VECTOR_SCORE_THRESHOLD`
- A2A：`A2A_ENABLED`、`A2A_PUBLIC_URL`、`A2A_AGENT_PATH`、超时、`A2A_ALLOWED_HOSTS`
- 认证：`JWT_SECRET`、算法、TTL

## 9. 错误处理

- 业务层只抛 `DomainError` 子类；
- 统一错误信封 `{"error": {"code","message","request_id"}}`；
- 未知异常 500 且不泄露堆栈；
- SSE 流内错误用 `event: error`，流正常关闭；
- A2A 错误按 JSON-RPC 错误对象返回，映射见 `docs/a2a.md`。

## 10. 测试方案

| 层次 | 内容 |
|---|---|
| 单元 | 配置、schema、工具封装、状态映射、SSRF 校验 |
| 集成 | FastAPI + 测试 PostgreSQL + 假 LLM |
| 契约 | Agent Card schema、JSON-RPC、SSE 事件 |
| E2E | 真模型 + 本地假远程 Agent（可选） |

骨架阶段无测试代码；`tests/README.md` 定义后续结构。

## 11. 交付物

本次初始化交付：

- 后端分层目录骨架与模块约定（空包、README、`.gitkeep`）；
- 依赖声明 `pyproject.toml`（未安装、无锁文件）；
- 环境变量模板 `.env.example`；
- PostgreSQL + pgvector 的 Docker Compose 与 initdb SQL（仅配置，未启动）；
- 前端 Vue 工程脚手架（create-vue `--bare`，未安装依赖）；
- 完整文档：README、架构、数据库、A2A、API、开发、路线图、本设计稿；
- 独立 Git 仓库（如执行初始化提交）。

明确不在本次范围：

- 任何 Python 实现代码（`main.py`、`core/`、`db/`、`agents/`、`modules/` 业务代码、`alembic/env.py`）；
- 任何业务接口与页面功能；
- LangGraph 图实现；
- A2A Server/Client 实现；
- 知识库入库与检索实现；
- 依赖安装、容器拉取/启动、数据库迁移；
- 测试用例与 CI。

## 12. 验收标准

1. 目录结构与本文档及 README 第 4 节一致；
2. `pyproject.toml` 与前端 `package.json` 的依赖声明与技术选型文档一致；
3. `.env.example` 覆盖文档中列出的配置项；
4. `docker compose config` 语法可校验（不要求拉取镜像或启动容器）；
5. 两个项目各自独立，文档可读、链接有效；
6. 不要求本次可启动、可构建或可迁移——运行验证留到 M1 编码阶段。

## 13. 关键决策记录

| 编号 | 决策 | 备选 | 结论 |
|---|---|---|---|
| ADR-001 | 向量库用 pgvector | Qdrant / Milvus / Chroma | 同库事务与运维优先，预留抽象 |
| ADR-002 | DB 层目录化 | 单文件 `db.py` | 按用户要求拆分，模型按领域分文件 |
| ADR-003 | 主图 + 远程 Agent 工具 | Supervisor 多图 / 外部编排 | 先简单可解释，复杂场景再演进 |
| ADR-004 | 本服务同时做 A2A Server | 只做 Client | 用户要求可被调用，双向互操作 |
| ADR-005 | checkpoint 独立 schema | public 混放 | 隔离增长快、可单独归档 |
| ADR-006 | 前端 Element Plus | Naive UI / 无组件库 | 团队熟悉度与生态优先 |
| ADR-007 | SSE 而非 WebSocket | WebSocket | 单向流式足够、实现简单、代理友好 |

## 14. 后续第一步

按 `docs/roadmap.md` M1 开始：先落 `users` / `conversations` / `messages` / `runs` 模型与迁移，再实现认证与最小对话闭环。编码前先读：

1. `README.md` 第 4–6 节；
2. `docs/architecture.md` 第 4、6 节；
3. `docs/database.md` 第 4 节；
4. `app/modules/README.md` 与 `app/agents/README.md`。
