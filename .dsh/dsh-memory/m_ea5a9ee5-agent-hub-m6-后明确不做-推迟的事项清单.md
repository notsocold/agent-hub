---
id: "m_ea5a9ee5"
title: "agent-hub M6 后明确不做/推迟的事项清单"
summary: "2026-11-11 盘点确认：A2A 入站、多实例/Redis/跨实例 resume（留 M7）、Helm、崩溃 run 续跑、消息正文自动清理、计费、管理员读记忆正文等均不在当前范围。"
kind: "decision"
importance: 4
tags: ["agent-hub","范围","决策","推迟"]
pinned: false
status: "active"
created: "2026-11-11T06:29:10.816Z"
modified: "2026-11-11T06:29:10.816Z"
revision: 1
origin: "auto"
sessionId: "session-19ed3c95-61c4-4fdc-961b-e2e254f421cf"
sessionTitle: "项目待办与前端对齐排查"
model: "deepseek-official/deepseek-flash"
---

M6 完成后盘点（docs/feedback/2026-11-11-project-status-review.md §3.5）确认的「明确不做 / 推迟」项，用来避免后续重复立项讨论：

- A2A 入站（本服务只做出站 Client，与既有需求变更一致）。
- 多实例、Redis、跨实例 resume —— 推迟到 M7；当前部署保持单实例单 worker。
- Helm 打包。
- 崩溃 run 续跑。
- 消息正文自动清理（保留策略待产品决策）。
- 计费。
- 管理员读取记忆正文。
