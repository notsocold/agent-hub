---
id: "m_c5312293"
title: "agent-hub A2A Card 拉取保留自研 httpx，SDK 仅做 schema 校验与传输"
summary: "Card 拉取的安全边界（SSRF/同源/重定向/大小上限）由自有 httpx 实现负责，SDK 的 parse_agent_card 只用于规范 schema 校验。"
kind: "decision"
importance: 4
tags: ["a2a","card","安全边界","ssrf","设计决策"]
pinned: false
status: "active"
created: "2026-11-10T03:40:29.539Z"
modified: "2026-11-10T03:40:29.539Z"
revision: 1
origin: "auto"
sessionId: "session-e3aa0116-6e08-44e5-976d-c1fe6c910b8a"
sessionTitle: "M3 开发"
model: "deepseek-official/deepseek-flash"
---

Card 拉取的 SSRF、同源、重定向、响应体上限等安全校验仍由 app/modules/a2a/card.py 的 httpx 实现承担，这是有意的安全边界，不算 SDK 偏离；新增 validate_card_schema() 用 SDK 的 parse_agent_card 做规范 schema 校验（deepcopy 防污染），再叠加技能等业务校验。

client.py 的响应体上限改为按 UTF-8 字节估算（传输层例外；对外 API 的长度口径仍以字符计，如指令 4000 字符）。
