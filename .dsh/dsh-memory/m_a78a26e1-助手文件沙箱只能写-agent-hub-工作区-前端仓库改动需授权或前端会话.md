---
id: "m_a78a26e1"
title: "助手文件沙箱只能写 agent-hub 工作区，前端仓库改动需授权或前端会话"
summary: "agent-hub-web 的文档改动无法由本仓库会话直接完成，需用户授权跨仓库写入或交给前端会话处理。"
kind: "project"
importance: 3
tags: ["工作环境","跨仓库","agent-hub-web"]
pinned: false
status: "active"
created: "2026-11-11T07:10:10.052Z"
modified: "2026-11-11T07:10:10.052Z"
revision: 1
origin: "auto"
sessionId: "session-19ed3c95-61c4-4fdc-961b-e2e254f421cf"
sessionTitle: "项目待办与前端对齐排查"
model: "deepseek-official/deepseek-flash"
---

助手运行时文件沙箱只允许写当前工作区（agent-hub），因此 agent-hub-web 仓库里的文档（如 README 状态、roadmap 里程碑状态）无法在本会话直接修改。处理方式：由用户显式授权跨仓库写入，或把改动清单交给前端会话执行。曾遗留的具体项：前端 README 的 F 系列状态、前端 roadmap 的 M6「进行中」。
