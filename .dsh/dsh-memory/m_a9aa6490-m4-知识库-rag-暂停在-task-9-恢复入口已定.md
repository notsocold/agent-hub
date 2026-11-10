---
id: "m_a9aa6490"
title: "M4 知识库 RAG 已交付完成；剩余待办为语义评测证据与生产 Embedding 策略"
summary: "M4 知识库 RAG 后端（Task 9–14 + 全分支审查修复）已完成并停在可复看状态，剩余两项待用户裁决/补证据。"
kind: "todo"
importance: 4
tags: ["agent-hub","m4","knowledge-rag","todo"]
pinned: false
status: "active"
created: "2026-11-10T08:36:51.025Z"
modified: "2026-11-10T14:00:29.904Z"
revision: 3
origin: "auto"
sessionId: "session-3bc6169d-3a64-443d-9cef-444da4c4c01a"
sessionTitle: "开始 M4 阶段"
model: "deepseek-official/deepseek-flash"
---

M4 知识库 RAG 后端已完成：Task 9–14 全部通过，最终整分支审查 0 Critical / 6 Important 已修复并复审通过，全量 339 passed / 1 skipped（曾连续 10 次全量稳定），ruff 通过，docs/openapi.json 重新导出。原「Task 11 需清除 chat/service.py 对 knowledge_categories 的无条件 403」已处理完毕。

仍待处理（用户需决策）：
1) 语义召回率 ≥ 90% 无证据 —— 本机无可用 Embedding key，roadmap 因此未打勾。补证据命令：KNOWLEDGE_EVAL_REAL=1 TEST_DATABASE_URL=postgresql+psycopg://devuser:[REDACTED]@localhost:54321/agent_hub_test .venv/bin/python -m pytest tests/integration/test_knowledge_eval.py -k semantic -v
2) 生产 Embedding 策略：实现按 spec 为「prod 缺 key 直接拒绝启动」，与实施计划原文「auto 一律回退 fake」冲突，等用户决定是否允许生产显式降级。

遗留未处理：审查残留 2 个 Low（派生 title 校验对「短显式 title + 长 filename」过度拦截；search 的 except ValueError 覆盖面略宽）；14 轮任务审查累积的 deferred minors 记在 .superpowers/sdd/2026-11-10-m4-knowledge-rag/progress.md（gitignore，未入库），其中 4 组建议在 M6 收口：扩展名常量三处重复、_estimate_tokens 两份、_commit_failed 二次提交失败路径、批量脚本 rebuild 语义。

下一步选项：前端 M4（知识库页/上传/检索调试/引用展示，需授权写 agent-hub-web）、进入 M5（审批恢复与安全加固）、或补语义评测证据。
