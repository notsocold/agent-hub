---
id: "m_638f250d"
title: "agent-hub Skill v1 定为纯指令包，可执行脚本列为后续方案"
summary: "agent-hub 的 Skill 能力第一阶段只做纯指令包，可执行脚本（含沙箱、资源限制、网络隔离等前置条件）单列为后续第二方案，不混入 v1 范围。"
kind: "decision"
importance: 4
tags: ["agent-hub","skill","范围界定","v1"]
pinned: false
status: "active"
created: "2026-11-05T15:38:59.694Z"
modified: "2026-11-05T15:38:59.694Z"
revision: 1
origin: "auto"
sessionId: "session-fcf98997-64ab-41d5-8e3e-08b306e565f6"
sessionTitle: "支持MCP、Skill与长期记忆配置"
model: "deepseek-official/deepseek-flash"
---

用户确认按「第一个方案」实现，并要求在文档中标注后续要实现第二个方案。

Skill 能力范围：
- v1：纯指令包（仅提示词/指令内容），不做任何代码执行。
- 后续方案（单列，不混入 v1）：可执行脚本型 Skill，文档需写清前置条件——沙箱隔离、资源限制、网络隔离。

文档要求：在需求/设计文档中明确标出该后续方案及其前置条件，避免与 v1 范围混淆。
