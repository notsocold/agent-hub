---
id: "m_e52f35a6"
title: "agent-hub A2A 入站请求用 CapabilityContext(user_id=None) 隔离用户级上下文"
summary: "A2A 入站调用构造无用户身份的 CapabilityContext，不注入任何用户级指令、记忆、技能与 MCP。"
kind: "decision"
importance: 4
tags: ["agent-hub","a2a","security","capability-context"]
pinned: false
status: "active"
created: "2026-11-05T16:00:04.536Z"
modified: "2026-11-05T16:00:04.536Z"
revision: 1
origin: "auto"
sessionId: "session-fcf98997-64ab-41d5-8e3e-08b306e565f6"
sessionTitle: "支持MCP、Skill与长期记忆配置"
model: "deepseek-official/deepseek-flash"
---

agent-hub 的 A2A 入站能力上下文定为 `CapabilityContext(user_id=None)`：外部 Agent 调用时不继承任何用户级 Instruction、长期记忆、Skill 与 MCP 配置，只有平台级（系统默认层）能力可用。理由：入站请求没有可信用户身份，注入用户级上下文会造成越权与数据泄漏。
