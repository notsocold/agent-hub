---
id: "m_4c883c07"
title: "agent-hub 部署/联调前必须 alembic upgrade head，测试 create_all 会掩盖迁移缺失"
summary: "测试库用 create_all 建表，导致开发库未跑迁移时测试全绿但真实服务上传返回 500；部署前必须执行 alembic upgrade head。"
kind: "project"
importance: 4
tags: ["agent-hub","alembic","migration","deployment"]
pinned: false
status: "active"
created: "2026-11-10T14:00:29.909Z"
modified: "2026-11-10T14:00:29.909Z"
revision: 1
origin: "auto"
sessionId: "session-3bc6169d-3a64-443d-9cef-444da4c4c01a"
sessionTitle: "开始 M4 阶段"
model: "deepseek-official/deepseek-flash"
---

真实 HTTP 联调时上传接口返回 500，而测试全绿：根因是开发库没有执行 M4 迁移（knowledge_documents / knowledge_chunks 未建），测试库用 create_all 建表把这个问题完全掩盖了。

处理与约定：执行 alembic upgrade head（M4 为 8a2f7c31e9d5 → c4e7a91b2f36，两表 + HNSW + GIN 就位）后恢复正常；该坑已写进 docs/development.md 的显眼位置。

后续规则：任何真实服务联调或部署前先跑 alembic upgrade head；遇到「测试通过但真实服务 500」，优先检查迁移是否应用到目标库，而不是先怀疑业务代码。
