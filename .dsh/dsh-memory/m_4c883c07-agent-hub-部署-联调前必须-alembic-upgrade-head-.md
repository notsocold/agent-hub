---
id: "m_4c883c07"
title: "agent-hub 部署/联调前必须 alembic upgrade head，测试 create_all 会掩盖迁移缺失"
summary: "测试库用 create_all 建表，导致开发库未跑迁移时测试全绿但真实服务上传返回 500；部署前必须执行 alembic upgrade head。"
kind: "project"
importance: 4
tags: ["agent-hub","部署","alembic","sse"]
pinned: false
status: "active"
created: "2026-11-10T14:00:29.909Z"
modified: "2026-11-11T06:29:10.813Z"
revision: 2
origin: "auto"
sessionId: "session-19ed3c95-61c4-4fdc-961b-e2e254f421cf"
sessionTitle: "项目待办与前端对齐排查"
model: "deepseek-official/deepseek-flash"
---

测试库用 create_all 建表，导致开发库未跑迁移时测试全绿但真实服务上传返回 500；部署前必须执行 alembic upgrade head，并用 alembic check 确认无新迁移。

其他部署约定（2026-11-11 盘点）：READINESS_CHECK_MIGRATIONS 只在生产 Compose 开启；服务为单实例单 worker（多实例/跨实例能力推迟到 M7）；反向代理的 SSE 读超时建议大于 900s。
