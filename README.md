# Agent Hub（agent-hub）

> 面向用户的多智能体对话服务：LangGraph 负责编排，Google A2A 协议负责连接外部智能体，PostgreSQL + pgvector 负责业务数据、会话记忆与知识检索。

配套前端：[agent-hub-web](../../NodeJsProjects/agent-hub-web)（Vue 3 + TypeScript + Vite）。

> **当前状态：M1 对话最小闭环已实现（2026-11-09）。**
> 已包含认证、会话/消息、聊天 SSE、运行与事件回放、审批最小接口、系统信息；
> 依赖已安装并通过 17 个契约测试，迁移已通过 `upgrade → downgrade → upgrade`。
> M1.5（Skill/记忆/Instruction/MCP/附件）与 M2–M6 仍未实现，能力开关会明确返回 `false`。

## 1. 项目定位

用户在前端与主智能体对话；主智能体在 LangGraph 状态图中自主决定：

- **直接回答**：通用对话，不调用外部能力；
- **查知识库**：通过 `search_knowledge` 工具做 RAG 检索，回答附来源；
- **调用远程智能体**：通过 A2A 协议把任务委派给其他智能体（如代码助手、数据分析、翻译），再把结果整合给用户；
- **请求人工确认**：高风险动作（写操作、外部副作用）触发 HITL 审批，用户确认后继续。

同时，本服务自身也是一个 A2A Server：发布 Agent Card，暴露 JSON-RPC 端点，允许其他智能体反向调用。

平台另提供**个人能力层**：账号级的 MCP 接入、Skill 技能包、长期记忆与自定义 Instruction。配置采用「系统默认 + 用户自定义」两层模型，运行时合并；会话内可以显式点名使用某个 Skill 或某个 MCP 服务器，只影响当次请求。设计见 [个人能力层设计](docs/superpowers/specs/2026-11-05-agent-capabilities-design.md)。

## 2. 架构（设计）

```mermaid
flowchart LR
    subgraph FE["前端 agent-hub-web"]
        UI["Vue 3 对话工作台"]
    end

    subgraph BE["后端 agent-hub"]
        API["FastAPI /api/v1<br/>REST + SSE"]
        CHAT["chat 模块<br/>流式编排"]
        GRAPH["LangGraph 主智能体"]
        TOOLS["工具层<br/>知识检索 / 远程 Agent / 审批"]
        A2AS["A2A Server<br/>Agent Card + JSON-RPC"]
        A2AC["A2A Client<br/>注册 / 调用 / 任务跟踪"]
        DB[("PostgreSQL 16<br/>业务表 + pgvector")]
        CKPT[("langgraph schema<br/>checkpoint 表")]
    end

    subgraph REMOTE["远程智能体"]
        R1["Agent A"]
        R2["Agent B"]
    end

    UI -->|"REST + SSE"| API
    API --> CHAT
    CHAT --> GRAPH
    GRAPH --> TOOLS
    TOOLS -->|"向量检索"| DB
    TOOLS -->|"A2A 调用"| A2AC
    A2AC -->|"JSON-RPC / SSE"| R1
    A2AC -->|"JSON-RPC / SSE"| R2
    GRAPH <-->|"会话记忆"| CKPT
    A2AS --> GRAPH
    R1 -->|"反向调用"| A2AS
```

## 3. 技术选型（设计基线）

| 层 | 选型 | 说明 |
|---|---|---|
| Web 框架 | FastAPI + Uvicorn | `create_app()` 工厂，REST + SSE |
| 智能体编排 | LangChain 1.x + LangGraph 1.x | 状态图、工具调用、中断、流式事件 |
| A2A 协议 | `a2a-sdk[fastapi]` 1.x | 实现 A2A 1.0 规范，兼容 0.3 |
| 业务库 | PostgreSQL 16 + SQLAlchemy 2（异步） + Alembic | 统一承载业务表 |
| 向量检索 | pgvector（HNSW，cosine） | 与业务表同库同事务 |
| 会话记忆 | `langgraph-checkpoint-postgres` | checkpoint 表放独立 `langgraph` schema |
| 模型 | OpenAI 兼容接口（默认 DeepSeek） | base_url / model 可配置 |
| Embedding | OpenAI 兼容接口 | 可切硅基流动、阿里百炼、本地 vLLM |
| 认证 | JWT + pwdlib(argon2) | access/refresh 双令牌 |
| 前端 | Vue 3 + TypeScript + Vite + Pinia + Vue Router | 见 `agent-hub-web` |
| 包管理 | uv（Python 3.13） / pnpm | 与现有项目保持一致 |

> 以上为设计选择，依赖已安装并锁定在 `uv.lock`；后续里程碑可在不破坏 M1 的前提下调整。

## 4. 目录结构

```text
agent-hub/
├── app/
│   ├── main.py              # create_app() / lifespan / CORS / 异常与请求 ID
│   ├── api/v1/              # /api/v1 聚合路由
│   ├── core/                # 配置、日志、错误、JWT、游标、限流、依赖
│   ├── db/                  # 数据库层（目录化，替代单文件 db.py）
│   │   ├── models/          # users/conversations/messages/runs/run_events/approvals...
│   │   └── vector/          # 向量存储抽象与 pgvector 实现（M4）
│   ├── agents/              # LangGraph 图、提示词、checkpoint、假模型
│   │   └── tools/
│   └── modules/             # 业务模块
│       ├── auth/            # 注册、登录、刷新轮换、账号资料
│       ├── conversations/   # 会话、消息历史与分页
│       ├── chat/            # SSE、run、事件、取消
│       ├── approvals/       # 审批查询与决定
│       ├── agents/          # 远程智能体（M2）
│       ├── a2a/             # A2A Server/Client（M2/M3）
│       ├── knowledge/       # 知识库（M4）
│       ├── tasks/           # A2A Task（M2）
│       └── system/          # 系统信息与健康检查
├── alembic/                 # 异步 env.py + M1 初始迁移
├── docker/initdb/           # 可选：使用 docker compose 时的扩展/schema 初始化 SQL
├── docker-compose.yml       # 可选：本机没有 PostgreSQL 时的备用编排
├── scripts/                 # 运维/导出脚本（grant_admin、export_openapi）
├── tests/                   # 单元 + 集成契约测试
├── data/                    # 知识库原始文档（规划）
├── docs/                    # 架构、数据库、A2A、API、开发、路线图
├── pyproject.toml           # 依赖声明（uv.lock 已生成）
├── .env.example             # 环境变量模板
└── README.md
```

说明：

- `app/db/` 是本次相对 `customer-service` 的关键调整：不再用单文件 `app/db.py`，而是 `base / engine / session / mixins / models / vector` 分目录；
- M1 已实现 `app/core`、`app/db`、`app/agents`、`app/modules/{auth,conversations,chat,approvals,system}`；
- 模块边界与后续文件规划见 `app/modules/README.md`、`app/agents/README.md` 和 `docs/architecture.md`。

## 5. 环境准备

> 本项目**不依赖 Docker**：默认使用使用方本机已有的 PostgreSQL。
> `DATABASE_URL` 指向已有实例即可；`docker-compose.yml` 仅作为没有现成 PostgreSQL 时的可选项。
> `pgvector` 扩展在 M4 才需要，M1 用普通 PostgreSQL 即可。

```bash
# 1. 配置环境变量
cp .env.example .env
# 把 DATABASE_URL 改成你本机已有 PostgreSQL 的连接串；
# 再填 JWT_SECRET；没有模型密钥时保持 LLM_PROVIDER=fake

# 2. 安装依赖
uv sync

# 3. 创建业务库（如果本机还没有 agent_hub 库）
createdb -h localhost -p 5432 -U postgres agent_hub   # 按实际用户/端口修改

# 4. 执行迁移
uv run alembic upgrade head

# 5. 启动服务
uv run uvicorn app.main:app --reload
curl http://localhost:8000/healthz
curl http://localhost:8000/api/v1/system/info   # 需要先登录拿 Bearer token
```

## 6. 当前状态（M1 · 对话最小闭环）

已完成：

- [x] 配置、日志、统一错误信封、`X-Request-ID`、CORS、限流
- [x] `users` / `refresh_tokens` / `conversations` / `messages` / `runs` / `run_events` / `approvals` / `audit_logs` / `system_settings` / `user_settings` 模型
- [x] Alembic `env.py` + M1 初始迁移（含 downgrade）
- [x] 注册、登录、刷新轮换、登出、`GET/PATCH /auth/me`、改密
- [x] 会话 CRUD、标题搜索、归档/软删、消息历史游标分页
- [x] LangGraph 主图 + PostgresSaver/InMemorySaver + 假流式模型
- [x] `POST /chat/stream` SSE（`run.started`、`message.*`、`X-Run-ID`、心跳、取消）
- [x] `GET /runs/{id}`、`GET /runs/{id}/events`、`POST /runs/{id}/cancel`
- [x] 审批列表/详情/决定（M1 无工具触发 interrupt）
- [x] `/healthz`、`/readyz`、`GET /system/info` 能力开关
- [x] `scripts/grant_admin.py`、`scripts/export_openapi.py`
- [x] 17 个 pytest 契约用例；真实 HTTP 流式与 Postgres checkpoint 验证

明确未包含（按路线图后续交付）：

- [ ] M1.5：Skill / 长期记忆 / Instruction / MCP / 附件（能力开关为 false）
- [ ] M2/M3：A2A 出站/入站、智能体页、任务页
- [ ] M4：知识库 + pgvector
- [ ] M5+：工作流、管理控制台、RBAC
- [ ] CI、Dockerfile、生产可观测性

## 7. 与 customer-service 的结构差异

| 维度 | customer-service | agent-hub |
|---|---|---|
| 数据库连接层 | 单文件 `app/db.py` | 目录 `app/db/`（base / engine / session / mixins / models / vector） |
| ORM 模型 | 各业务模块内 `models.py` | 统一 `app/db/models/<领域>.py`，按领域拆分 |
| 业务模型 | 客服：订单、审批、RBAC | 对话平台：会话、消息、run、远程 Agent、A2A Task、知识库 |
| 向量存储 | FAISS 本地索引 | PostgreSQL + pgvector，与业务同库 |
| 会话记忆 | SqliteSaver | LangGraph PostgresSaver（独立 schema） |
| 多智能体 | 单 agent + 工具 | 主图 + A2A 出站/入站 |
| 前端 | 无 | 独立 Vue 项目 `agent-hub-web` |

## 8. 文档索引

| 文档 | 内容 |
|---|---|
| [架构设计](docs/architecture.md) | 组件、时序、分层规则、关键决策 |
| [数据库设计](docs/database.md) | ER 图、表结构、pgvector 索引、迁移规范 |
| [A2A 集成](docs/a2a.md) | Agent Card、服务端/客户端、任务状态、安全 |
| [API 契约](docs/api.md) | REST 端点、SSE 事件、错误码 |
| [开发指南](docs/development.md) | 环境、命令、编码规范、联调、FAQ |
| [路线图](docs/roadmap.md) | M0–M6 里程碑与验收标准（含 M1.5 个人能力层） |
| [初始化设计稿](docs/superpowers/specs/2026-11-04-agent-hub-init-design.md) | 本次初始化的决策记录 |
| [个人能力层设计](docs/superpowers/specs/2026-11-05-agent-capabilities-design.md) | MCP / Skill / 长期记忆 / Instruction 的设计与决策 |
| [M1 前端契约反馈](docs/feedback/2026-11-09-frontend-contract-feedback.md) | 与前端交接包的差异、待拍板决策、联调步骤 |
| [OpenAPI](docs/openapi.json) | M1 实际接口契约（由 FastAPI 导出） |

## 9. 下一步

M1 已完成，按 `docs/roadmap.md` 继续：

1. 与前端对齐 [M1 契约反馈](docs/feedback/2026-11-09-frontend-contract-feedback.md) 中的 12 个点；
2. 进入 M1.5-A：指令、长期记忆、技能（`system_settings` / `user_settings` / `skills` / `skill_files` 已建基础表）；
3. 再做 M1.5-B：MCP + 附件 + 完整审批链路；
4. 每一步先更新设计文档与 `docs/openapi.json`，再写代码。

编码前建议阅读：

1. 本 README 第 4–6 节；
2. `docs/architecture.md` 第 4、6 节；
3. `docs/database.md` 第 4 节；
4. `app/modules/README.md` 与 `app/agents/README.md`。
