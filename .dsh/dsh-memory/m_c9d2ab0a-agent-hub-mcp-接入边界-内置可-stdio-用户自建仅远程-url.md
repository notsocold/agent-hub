---
id: "m_c9d2ab0a"
title: "agent-hub MCP 接入边界：内置可 stdio，用户自建仅远程 URL"
summary: "agent-hub 的 MCP 支持定为：系统内置 MCP 允许 stdio 传输，用户自建 MCP 只允许远程 URL，stdio 开关默认关闭。"
kind: "decision"
importance: 4
tags: ["agent-hub","mcp","security","design-decision"]
pinned: false
status: "active"
created: "2026-11-05T15:36:26.172Z"
modified: "2026-11-05T15:36:26.172Z"
revision: 1
origin: "auto"
sessionId: "session-fcf98997-64ab-41d5-8e3e-08b306e565f6"
sessionTitle: "支持MCP、Skill与长期记忆配置"
model: "deepseek-official/deepseek-flash"
---

agent-hub 的 MCP 设计边界（用户已确认按建议执行）：
- 系统内置 MCP：允许 stdio 传输方式。
- 用户自建 MCP：只允许远程 URL（HTTP/SSE 等），不开放 stdio。
- stdio 开关：默认关闭，需显式开启。
理由/影响：stdio 需要平台为用户进程跑沙箱、限资源、隔网络，复杂度与安全风险高，因此对用户自建部分收紧到远程 URL。该决定在需求文档阶段确立，尚未进入编码。
