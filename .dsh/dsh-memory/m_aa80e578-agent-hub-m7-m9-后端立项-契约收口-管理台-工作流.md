---
id: "m_aa80e578"
title: "agent-hub M7–M9 后端立项：契约收口 / 管理台 / 工作流"
summary: "2026-11-11 把后端「明确后续要做」的能力立项为 M7/M8/M9 三个可独立验收的里程碑，需求触发项收进候选池不占编号。"
kind: "decision"
importance: 4
tags: ["roadmap","M7","M8","M9","立项"]
pinned: false
status: "active"
created: "2026-11-11T07:10:10.049Z"
modified: "2026-11-11T07:10:10.049Z"
revision: 1
origin: "auto"
sessionId: "session-19ed3c95-61c4-4fdc-961b-e2e254f421cf"
sessionTitle: "项目待办与前端对齐排查"
model: "deepseek-official/deepseek-flash"
---

用户要求对「后端设计还没做但明确后续要做」的部分正式立项（本轮只做设计与计划，不写业务代码）。拆成三个里程碑：
- M7 契约补齐与安全收口（4–5 天，无依赖）：事件回放补 `event` 字段 + SSE 续流端点；agents/tasks 写操作 RBAC（复用已存在的 `AdminUser`，无迁移）；审批事件字段；A2A 授权 URL 白名单（写入点在 `a2a/tool.py` 的 `auth_required` 分支，不是 tasks/service.py 输出层）；契约/OpenAPI/前端 handoff。
- M8 管理台后端（原 X2，5–7 天，依赖 M7 的 RBAC）：用户/角色/状态、平台指令、系统技能/MCP/全局 Agent、只读概览、403 与审计；预计无迁移。
- M9 工作流后端（原 X1，10–15 天，依赖 M7 事件流）：轻量步骤模板 MVP（检索/工具/A2A/审批/回答）、CRUD/运行/历史、`workflow.step` 事件；先做 1 天 spike。
候选池（不占编号、触发条件驱动）：多实例/Redis/跨实例 resume、通用 `POST /runs/{id}/resume`、真实 MinIO 联调、语义召回评测取证、对象存储孤儿巡检、上传异步入库、MCP 连接池、A2A 巡检退避、MCP stdio 沙箱、消息正文保留策略。
文档产出：docs/superpowers/specs/2026-11-11-m7-m9-backend-roadmap-design.md（目标/范围/非目标/关键决策/验收/依赖风险）、docs/superpowers/plans/2026-11-11-m7-contract-security.md（M7 Task 1–7 实施计划）、roadmap.md 与 docs/feedback/2026-11-11-project-status-review.md 已同步归属。
