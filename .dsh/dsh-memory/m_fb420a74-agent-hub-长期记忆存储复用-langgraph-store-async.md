---
id: "m_fb420a74"
title: "agent-hub 长期记忆存储复用 LangGraph Store（AsyncPostgresStore），不自研存储"
summary: "agent-hub 的长期记忆直接用 LangGraph Store 的 Postgres 实现做存储层，插件只管写入策略与 REST 管理，表结构由框架 setup() 管理，不走 Alembic。"
kind: "decision"
importance: 4
tags: ["agent-hub","长期记忆","langgraph","postgres","存储选型"]
pinned: false
status: "active"
created: "2026-11-05T15:41:46.022Z"
modified: "2026-11-05T15:41:46.022Z"
revision: 1
origin: "auto"
sessionId: "session-fcf98997-64ab-41d5-8e3e-08b306e565f6"
sessionTitle: "支持MCP、Skill与长期记忆配置"
model: "deepseek-official/deepseek-flash"
---

agent-hub 长期记忆不自己发明存储，复用 `langgraph-checkpoint-postgres` 的 `AsyncPostgresStore`：
- 数据模型：命名空间 + key 的 KV，value 为 JSONB，带 created_at/updated_at；配 `index={dims, embed, fields}` 后可用 pgvector 做语义检索（`asearch(query, filter, limit, offset)`）；不带 query 时按 updated_at DESC 列表，正好做设置页「最近更新」；支持 TTL。
- 建表：`setup()` 幂等创建 `store` / `store_vectors`，与 checkpoint 一样由框架管理，不使用 Alembic 迁移。
- 部署约束：表放 `langgraph` schema，连接串需带 `search_path=langgraph,public`（`vector` 类型在 public）。
- 拟定命名约定：namespace `("memories", user_id)`，key `mem_<ulid>`；value 形如 `{kind, content, title?, pinned, status, source:{conversation_id, message_id}, tags}`，用 `kind` 做 filter 过滤。
- 读写路径：图内用 `BaseStore` 的 `aput`/`asearch`；设置页由 `modules/memories` 包一层 REST（列表/搜索/编辑/删除/停用/清空）。
- 边界：Store 只解决「存和查」，不决定「何时写、写什么」；写入策略（v1 是否只做模型显式 `save_memory` 工具 + 用户手动管理、自动提取放 v2）仍需与用户确认后再定。
