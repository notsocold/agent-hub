# 业务模块约定

`app/modules/` 按业务能力分包，每个模块自成闭环；模块之间只通过 service 层或 `app/core/deps.py` 暴露的依赖交互，禁止跨模块直接 import 对方的 `router`。

## 模块清单

| 模块 | 职责 | 规划文件 |
|---|---|---|
| `auth/` | 注册、登录、刷新令牌、当前用户、JWT 鉴权依赖 | `schemas.py` `repo.py` `service.py` `router.py` |
| `conversations/` | 会话创建/列表/重命名/归档/删除、消息历史分页 | `schemas.py` `repo.py` `service.py` `router.py` |
| `chat/` | 用户消息 → LangGraph → SSE 流式；运行取消；能力装配与工具执行（M1.5） | `schemas.py` `service.py` `router.py` `capabilities.py` `tools.py` `registry.py` `events.py` `bus.py` |
| `agents/` | 远程智能体注册、Agent Card 拉取与缓存、健康检查、能力目录（M2） | `schemas.py` `repo.py` `service.py` `router.py` |
| `a2a/` | A2A 服务端（AgentExecutor）与客户端封装、LangChain 工具桥接（M2/M3） | `card.py` `executor.py` `server.py` `client.py` `tool.py` `registry.py` |
| `knowledge/` | 知识库文档上传、解析、切分、Embedding、重建索引、检索接口（M4） | `schemas.py` `repo.py` `service.py` `router.py` |
| `tasks/` | run 与 A2A Task 的查询、取消、事件回放（M2） | `schemas.py` `repo.py` `service.py` `router.py` |
| `approvals/` | 高风险工具/远程调用的人工审批（HITL）；决定唤醒运行（M1.5 起可用） | `schemas.py` `repo.py` `service.py` `router.py` |
| `attachments/` | 附件上传（sha256 去重）、列表、下载、软删（M1.5） | `schemas.py` `service.py` `router.py` |
| `instructions/` | 平台默认/用户覆盖解析、生效预览、审计（M1.5） | `schemas.py` `service.py` `router.py` |
| `memories/` | 长期记忆 CRUD/检索/导出与聊天注入、save_memory 保存策略（M1.5） | `schemas.py` `service.py` `router.py` |
| `skills/` | 技能包导入校验、存储、启停、参考文件读取、系统技能同步（M1.5） | `schemas.py` `service.py` `router.py` `importer.py` |
| `mcp/` | MCP 服务器注册、JSON-RPC 客户端、工具发现、SSRF 防护、聊天工具装配（M1.5） | `schemas.py` `service.py` `router.py` `client.py` |

## 文件分层

```
router.py     HTTP 边界：参数解析、鉴权依赖、调用 service、返回 DTO
schemas.py    Pydantic 请求/响应模型，不复用 ORM 模型
service.py    业务规则与事务边界；不依赖 FastAPI Request/Response
repo.py       纯持久化操作；复杂查询放这里，SQL 不散落在 service
```

- **ORM 模型不放在模块内**：统一放 `app/db/models/<领域>.py`，由 `app/db/models/__init__.py` 汇总导入，保证 Alembic 能发现。
- **repo 可省略**：纯 CRUD 且没有复杂查询时，service 可直接用 `AsyncSession`；一旦出现跨表查询/锁/批量操作，再补 repo。
- **依赖注入**：数据库会话用 `app.db.session.get_db`；当前用户用 `app.core.deps.get_current_user`（待实现）。

## 依赖方向

```
router → service → repo → db.models
router → schemas
service → schemas（类型化输入输出）
modules/a2a → agents（LangGraph 图与工具）
modules/chat → agents（图执行、提示词装配）
modules/chat → modules/{instructions,memories,skills,mcp,attachments}（能力装配）
modules/chat/tools.py → modules/{memories,skills,mcp}（工具执行）
modules/mcp → app/core/ssrf（SSRF 与凭据加密）
modules/skills → app/agents/skills（内置技能仓库目录，仅启动时同步）
```

禁止反向依赖：`agents/` 不 import `modules/`；`db/` 不 import `modules/`。
