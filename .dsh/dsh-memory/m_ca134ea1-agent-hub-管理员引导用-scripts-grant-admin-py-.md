---
id: "m_ca134ea1"
title: "agent-hub 管理员引导用 scripts/grant_admin.py，系统项用 force_disabled 硬停用"
summary: "管理员身份改为运维脚本显式授权（不做注册邮箱自动提权），并新增 force_disabled 让管理员硬停用系统项、压过用户覆盖。"
kind: "decision"
importance: 4
tags: ["agent-hub","admin","security","配置模型"]
pinned: false
status: "active"
created: "2026-11-05T16:00:04.525Z"
modified: "2026-11-05T16:00:04.525Z"
revision: 1
origin: "auto"
sessionId: "session-fcf98997-64ab-41d5-8e3e-08b306e565f6"
sessionTitle: "支持MCP、Skill与长期记忆配置"
model: "deepseek-official/deepseek-flash"
---

agent-hub 的两条管理边界决策：
1) 管理员引导：取消“注册邮箱命中白名单即自动提权”的方案，改为运维执行 `scripts/grant_admin.py` 显式授权，避免邮箱可控导致的提权风险。
2) 系统项停用：设置项新增 `force_disabled`，管理员可对系统内置 MCP/Skill 等做硬停用，优先级压过用户的个人覆盖；仓库下架的内置技能也用它防止用户侧“复活”已下架内容。
