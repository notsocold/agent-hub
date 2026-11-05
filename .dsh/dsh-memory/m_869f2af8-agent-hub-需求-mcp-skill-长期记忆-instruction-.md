---
id: "m_869f2af8"
title: "agent-hub 需求：MCP、Skill、长期记忆、Instruction 四大能力 + 两层配置模型"
summary: "agent-hub 本阶段做四大能力（MCP、Skill、长期记忆、Instruction），配置为账号级并由「系统默认 + 用户自定义」两层在运行时合并，会话内可显式点名使用 Skill/MCP。"
kind: "project"
importance: 4
tags: ["agent-hub","需求","配置模型","mcp","skill"]
pinned: false
status: "active"
created: "2026-11-05T15:22:41.675Z"
modified: "2026-11-05T15:33:00.443Z"
revision: 3
origin: "auto"
sessionId: "session-fcf98997-64ab-41d5-8e3e-08b306e565f6"
sessionTitle: "支持MCP、Skill与长期记忆配置"
model: "deepseek-official/deepseek-flash"
---

agent-hub 本阶段做四大能力：MCP、Skill、长期记忆、Instruction。
配置模型：账号级配置，采用「系统默认 + 用户自定义」两层，在运行时合并。
会话层语义（用户确认）：Skill / MCP 的启用配置都在账号级；但用户可以在单次会话中显式使用某个 Skill 或某个 MCP 工具，该指定只影响当次请求，不修改账号级配置。
