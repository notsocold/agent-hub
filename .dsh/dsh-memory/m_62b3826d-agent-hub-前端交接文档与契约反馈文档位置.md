---
id: "m_62b3826d"
title: "agent-hub 前端交接/联调文档与契约修复要点"
summary: "agent-hub 与 agent-hub-web 的交接/反馈/热修文档位置，以及已对齐的契约要点（cursor 分页、错误信封、seed 账号、OpenAPI 声明）。"
kind: "reference"
importance: 4
tags: ["agent-hub","contract","openapi","frontend-handoff","docs"]
pinned: false
status: "active"
created: "2026-11-09T09:10:11.591Z"
modified: "2026-11-09T13:50:20.248Z"
revision: 3
origin: "auto"
sessionId: "session-fb5c27ae-a636-43ea-8da2-2813dbe9f022"
sessionTitle: "Frontend contract integration feedback"
model: "deepseek-official/deepseek-flash"
---

agent-hub 前后端联调文档统一放在 docs/feedback/ 下，按日期命名：
- 2026-11-09-frontend-contract-feedback.md（前端契约反馈，第 5 节列出阻断项，第 10 节为复验清单）
- 2026-11-09-frontend-integration-response.md（前端联调回应）
- 2026-11-09-backend-hotfix-response.md（后端逐条回复 + 复验命令 + 前端复验清单）

契约真相以 docs/api.md（含 §7.6–§7.12：分页契约、运行事件回放、resume 错误码、message.done 字段、错误可观测性、限流配置、M1 热修记录）和导出的 docs/openapi.json 为准；运行中实例的 /openapi.json 应与导出的 openapi.json 完全一致，可据此判断是否加载了最新代码。

已对齐要点：cursor 分页（会话/消息/审批）、统一错误信封（error.code/message/request_id，details 已建模）、SSE chat/stream 的事件顺序与断线恢复说明、X-Run-ID / X-Request-ID 响应头声明、所有业务端点统一声明 400/401/403/404/409/413/422/429/500、capabilities.agents/tasks 键始终返回且 M1 恒为 false、resume 错误码 run_not_resumable、开发账号 seed 脚本。
