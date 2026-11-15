---
id: "m_d8990329"
title: "agent-hub M8 后待办：R8 口径产品确认、前端接入 M7/M8、M9 spike"
summary: "M7/M8 后端已完成，仍需产品确认 R8 全局 Agent 口径，前端接入 M7/M8 契约，M9 工作流先做 1 天 spike。"
kind: "todo"
importance: 3
tags: ["agent-hub","todo","M7","M8","M9","前端"]
pinned: false
status: "active"
created: "2026-11-11T16:01:51.212Z"
modified: "2026-11-11T16:01:51.212Z"
revision: 1
origin: "auto"
sessionId: "session-4c5efa4f-7308-4b93-a054-c5e148a65852"
sessionTitle: "实现M7和M8功能"
model: "deepseek-official/deepseek-flash"
---

M7、M8 后端实现、审查与本地提交已完成（未推送）。剩余待办：

1) R8 全局 Agent 口径需产品最终确认（当前按「全部未软删」实现，而非 owner_user_id IS NULL）。
2) 前端尚未接入：M7 的续流/403 语义/审批事件新字段，M8 的管理台页面；后端契约可独立联调，交接文档为 docs/feedback/2026-11-11-m7-frontend-handoff.md 与 docs/feedback/2026-11-11-m8-frontend-handoff.md。
3) M9 工作流已立项未开始，按计划先做 1 天 spike。
4) 候选池（不占里程碑编号）：跨实例/Redis/跨实例 resume 等，最新汇总见 docs/feedback/2026-11-11-project-status-review.md。
