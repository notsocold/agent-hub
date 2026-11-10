---
id: "m_153cc36c"
title: "agent-hub M4 生产 Embedding 策略：缺 key 拒绝启动（偏离计划的 auto 回退 fake）"
summary: "按设计 spec 实现为生产环境缺少 Embedding key 直接拒绝启动，与实施计划的「auto 一律回退 fake」冲突，属待用户裁决的显式偏离。"
kind: "project"
importance: 3
tags: ["agent-hub","m4","embedding","decision"]
pinned: false
status: "active"
created: "2026-11-10T14:00:29.910Z"
modified: "2026-11-10T14:00:29.910Z"
revision: 1
origin: "auto"
sessionId: "session-3bc6169d-3a64-443d-9cef-444da4c4c01a"
sessionTitle: "开始 M4 阶段"
model: "deepseek-official/deepseek-flash"
---

M4 的 Embedding 提供方支持 fake/真实切换。实施时按设计 spec 定为：生产环境缺少真实 Embedding key 时直接拒绝启动，不做静默降级；而实施计划原文写的是「auto 模式一律回退 fake」。这是一处有意偏离，已在交付报告与进度台账（.superpowers/sdd/2026-11-10-m4-knowledge-rag/progress.md）中记录，等用户确认。

若用户希望生产允许降级（至少显式开启），改动成本很低，现在改最省。

相关：测试与评测中 conftest 强制 fake，Task 13 曾暴露「即使有真实 key，语义组也永远 skip」的偏差并已修复；离线评测链路组 15/15 top-1 命中，语义组在 fake 下全部未命中（作为负向守卫）。
