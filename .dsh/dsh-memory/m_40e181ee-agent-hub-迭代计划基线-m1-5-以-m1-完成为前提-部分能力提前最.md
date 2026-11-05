---
id: "m_40e181ee"
title: "agent-hub 迭代计划基线：M1.5 以 M1 完成为前提，部分能力提前最小切片"
summary: "agent-hub 的 M1.5 里程碑依赖 M1 完成，并把 tool_invocations、audit_logs、approvals 与共享 SSRF 校验作为最小切片提前落地，M2/M5 范围不变。"
kind: "decision"
importance: 4
tags: ["agent-hub","roadmap","milestone","架构决策"]
pinned: false
status: "active"
created: "2026-11-05T16:00:04.522Z"
modified: "2026-11-05T16:00:04.522Z"
revision: 1
origin: "auto"
sessionId: "session-fcf98997-64ab-41d5-8e3e-08b306e565f6"
sessionTitle: "支持MCP、Skill与长期记忆配置"
model: "deepseek-official/deepseek-flash"
---

agent-hub 设计文档确定计划基线：M1.5 必须在 M1 完成的前提下启动；为避免后续返工，`tool_invocations`、`audit_logs`、`approvals` 三张表与共享 SSRF 校验逻辑以最小切片提前到 M1.5 落地；M2 与 M5 的原定范围保持不变。理由：审计、调用记录与审批是 MCP/Skill 执行的横切依赖，晚做会导致多次改表与改调用链。
