---
id: "m_0e1102ce"
title: "agent-hub M4 实施约定：不用工作树、沙箱依赖与错误语义 Ruling"
summary: "用户关闭了工作树功能，后续直接在 master 实施；沙箱下用 UV_CACHE_DIR + .venv/bin/python 装依赖跑测试，并已定下错误语义、PDF/rebuild/fixture 等实施约定。"
kind: "decision"
importance: 4
tags: ["agent-hub","convention","sandbox","worktree","testing"]
pinned: false
status: "active"
created: "2026-11-10T08:36:51.045Z"
modified: "2026-11-10T08:36:51.045Z"
revision: 1
origin: "auto"
sessionId: "session-3bc6169d-3a64-443d-9cef-444da4c4c01a"
sessionTitle: "开始 M4 阶段"
model: "deepseek-official/deepseek-flash"
---

执行方式：采用 subagent-driven 的 SDD 流程（ledger + 任务简报 + 实现报告 + 审查包 + 修复轮）。用户已关闭工作树功能，后续不再 create_worktree，直接在 master 上实施。

环境（沙箱限制）：uv 缓存目录只读，uv sync 会失败；装依赖用 UV_CACHE_DIR=… uv add，跑测试用 .venv/bin/python -m pytest。.env 永不提交，只提交 .env.example（worktree 需手动补 .env 与 .venv）。新 worktree/子目录需补 .superpowers/ 忽略规则。

错误语义：请求级错误（超限 413、扩展名非法）必须抛 4xx；内容级失败（解析失败、向量化失败）落为 failed 文档，不抛 5xx。

其他 Ruling：pypdf 无法写中文 PDF，PDF 测试样本只用 ASCII；rebuild(scope="own") 指所有用户的私有文档，返回的 documents 是处理总数、failed 是其中失败数；六个测试 fixture 集中在 conftest 一次成型，命名统一为 seeded_docs / admin_auth，任务私有数据放测试文件内。
