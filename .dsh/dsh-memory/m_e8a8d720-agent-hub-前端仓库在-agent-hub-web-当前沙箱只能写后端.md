---
id: "m_e8a8d720"
title: "agent-hub 前端仓库在 agent-hub-web，当前沙箱只能写后端"
summary: "前端 Vue 代码位于 ~/work/NodeJsProjects/agent-hub-web，不在 agent-hub 工作区内，当前会话的文件写入权限只覆盖 agent-hub 后端仓库。"
kind: "project"
importance: 3
tags: ["agent-hub","前端","工作区约束"]
pinned: false
status: "active"
created: "2026-11-10T06:29:28.081Z"
modified: "2026-11-10T06:29:28.081Z"
revision: 1
origin: "auto"
sessionId: "session-3bc6169d-3a64-443d-9cef-444da4c4c01a"
sessionTitle: "开始 M4 阶段"
model: "deepseek-official/deepseek-flash"
---

agent-hub 的前端在独立仓库 ~/work/NodeJsProjects/agent-hub-web（Vue），不在后端仓库 agent-hub 的工作区内。当前沙箱的写权限只有 agent-hub，因此涉及前端页面的改动只能给出契约与建议、由用户自行执行，或另行授权后再动。前后端接口契约类讨论需以该仓库的 types 与交接文档为准。
