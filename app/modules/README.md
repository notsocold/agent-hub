# 业务模块约定

`app/modules/` 按业务能力分包，每个模块自成闭环；模块之间只通过 service 层或 `app/core/deps.py` 暴露的依赖交互，禁止跨模块直接 import 对方的 `router`。

## 模块清单

| 模块 | 职责 | 规划文件 |
|---|---|---|
| `auth/` | 注册、登录、刷新令牌、当前用户、JWT 鉴权依赖 | `schemas.py` `repo.py` `service.py` `router.py` |
| `conversations/` | 会话创建/列表/重命名/归档/删除、消息历史分页 | `schemas.py` `repo.py` `service.py` `router.py` |
| `chat/` | 用户消息 → LangGraph → SSE 流式；运行取消；中断恢复 | `schemas.py` `service.py` `router.py` |
| `agents/` | 远程智能体注册、Agent Card 拉取与缓存、健康检查、能力目录 | `schemas.py` `repo.py` `service.py` `router.py` |
| `a2a/` | A2A 服务端（AgentExecutor）与客户端封装、LangChain 工具桥接 | `card.py` `executor.py` `server.py` `client.py` `tool.py` `registry.py` |
| `knowledge/` | 知识库文档上传、解析、切分、Embedding、重建索引、检索接口 | `schemas.py` `repo.py` `service.py` `router.py` |
| `tasks/` | run 与 A2A Task 的查询、取消、事件回放 | `schemas.py` `repo.py` `service.py` `router.py` |
| `approvals/` | 高风险工具/远程调用的人工审批（HITL） | `schemas.py` `repo.py` `service.py` `router.py` |
| `mcp/` | MCP 服务器注册、连接与工具发现、工具桥接、健康检查 | `schemas.py` `repo.py` `service.py` `router.py` `client.py` `tool.py` |
| `skills/` | 本地技能包导入、校验、存储、激活与参考文件读取 | `schemas.py` `repo.py` `service.py` `router.py` `importer.py` |
| `memories/` | 长期记忆的保存策略、检索注入、管理接口 | `schemas.py` `service.py` `router.py` |
| `instructions/` | 平台默认/用户覆盖解析、生效预览、审计 | `schemas.py` `repo.py` `service.py` `router.py` |

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
modules/chat → agents（图执行）
modules/chat → modules/{instructions,memories,skills,mcp}（能力目录装配）
modules/mcp → agents（MCP 工具定义）
modules/skills → agents（技能正文注入）
```

禁止反向依赖：`agents/` 不 import `modules/`；`db/` 不 import `modules/`。
