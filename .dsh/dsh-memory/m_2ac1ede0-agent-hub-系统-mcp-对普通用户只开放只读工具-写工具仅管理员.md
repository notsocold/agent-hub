---
id: "m_2ac1ede0"
title: "agent-hub 系统 MCP 对普通用户只开放只读工具，写工具仅管理员"
summary: "按方案 A 定稿：系统 MCP 的写/破坏性工具仅管理员可用，普通用户只能看到并调用只读工具；用户自建 MCP 的写工具由本人审批。"
kind: "decision"
importance: 4
tags: ["mcp","security","permissions","agent-hub"]
pinned: false
status: "active"
created: "2026-11-05T16:23:31.029Z"
modified: "2026-11-05T16:23:31.029Z"
revision: 1
origin: "auto"
sessionId: "session-fcf98997-64ab-41d5-8e3e-08b306e565f6"
sessionTitle: "支持MCP、Skill与长期记忆配置"
model: "deepseek-official/deepseek-flash"
---

agent-hub 系统 MCP 的工具开放规则按方案 A 定稿：普通用户只开放只读工具，写/破坏性工具仅管理员可用（此为产品口径，需在工具目录过滤、工具列表标记与平台凭据最小权限三处落实，而不是仅靠提示词约束）。对照：用户自建 MCP 的写工具由用户本人审批后可用。工具命名约定为 mcp__<slug>__<tool>。
