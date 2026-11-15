---
id: "m_5ed13b2e"
title: "agent-hub M5 安全加固约定：<untrusted> 包裹、secret: 解析、聊天限流"
summary: "M5 定下注入防护、平台密钥与限流的实现口径：外部内容统一包 <untrusted>、输出落库前打码、secret: 写入按角色/运行时无条件解析、聊天 30 次/60s 可配。"
kind: "decision"
importance: 4
tags: ["agent-hub","M5","security","prompt-injection","secrets","rate-limit"]
pinned: false
status: "active"
created: "2026-11-10T16:01:41.754Z"
modified: "2026-11-10T16:01:41.754Z"
revision: 1
origin: "auto"
sessionId: "session-a4bcdcb6-3e62-4ad3-8b77-2625ae552a80"
sessionTitle: "M5 implementation kickoff"
model: "deepseek-official/deepseek-flash"
---

- 提示词注入防护在 app/core/prompt_guard.py：规则检测命中 → 写审计 + 本轮提示加固；MCP/A2A/知识库/技能的结果（**含工具异常原文**）统一包 <untrusted>；知识库「未找到/错误/未启用」系统文案不包裹，只包真实检索片段。
- 输出侧密钥打码统一在落库前 _redact_before_store，覆盖正常、挂起、取消、失败所有路径。
- 平台密钥：platform_secrets 表 + /admin/secrets CRUD + secret:NAME 引用；写入时按**提交者角色**校验，运行时**无条件解析**；接口与审计不出现明文；503 错误码统一为 secret_store_unavailable。
- 聊天限流：默认 30 次/60s/用户（可配），429 rate_limited 带 retry_after_seconds。
- 审计检索：GET /audit-logs（用户自查）+ GET /admin/audit-logs（管理员全局），支持 actor/action/资源/时间/游标分页。
