---
id: "m_0464a21e"
title: "agent-hub 长度口径：API 用字符（指令 4000 字符），token 用保守系数做预算"
summary: "对外 API 一律以字符为单位（如指令上限 4000 字符），内部预算用保守系数估算 token，技能在导入时即校验 ≤8000 tokens。"
kind: "decision"
importance: 3
tags: ["agent-hub","api-contract","token","skill"]
pinned: false
status: "active"
created: "2026-11-05T16:00:04.534Z"
modified: "2026-11-05T16:00:04.534Z"
revision: 1
origin: "auto"
sessionId: "session-fcf98997-64ab-41d5-8e3e-08b306e565f6"
sessionTitle: "支持MCP、Skill与长期记忆配置"
model: "deepseek-official/deepseek-flash"
---

agent-hub 明确长度计量口径：对外 API 契约统一用字符数（例如 Instruction 上限 4000 字符），token 仅在内部做预算时用保守换算系数估算；技能（Skill）在**导入阶段**就校验内容 ≤ 8000 tokens，而不是等到激活时才拒绝，避免“能导入却不能激活”的体验问题。
